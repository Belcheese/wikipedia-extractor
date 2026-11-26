"""
wikipedia_extractor_core.py
===========================

Logic layer for the ISOM3400 Lab 12 Streamlit app.

This module contains **no user interface code**. It never calls print(),
never calls input(), never opens a file, and never imports streamlit.
Everything that can fail sets `self.error` on the extractor instance so the
UI layer can decide how to show it.

Public API
----------
    WikipediaExtractor(topic)   - fetches and parses on construction
        .title                  - resolved article title ("" on failure)
        .url                    - resolved article URL   ("" on failure)
        .sections               - list[Section]
        .error                  - str or None
        .section_titles()       - list[str], the multiselect options
        .extract(title)         - str, one rendered section

    to_title(topic)             - normalise user input into a searchable title
    safe_filename(title)        - "Singapore_extract.txt"
    numbered_filename(name, existing_names) - "Singapore_extract (2).txt"
    format_txt(title, url, sections) - the complete .txt file content

Author: Mo Yeuk Hei (21232498)
"""

import re
import textwrap
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

WIKI_BASE = "https://en.wikipedia.org/wiki/"
SEARCH_URL = "https://en.wikipedia.org/w/index.php"

HEADERS = {
    "User-Agent": "ISOM3400-Coursework/1.0 (HKU student project; contact: student@connect.hku.hk)"
}

TIMEOUT_SECONDS = 30
MAX_ATTEMPTS = 3

LICENCE_NAME = "CC BY-SA 4.0"
LICENCE_URL = "https://creativecommons.org/licenses/by-sa/4.0/"

# Sections that are navigation furniture rather than article content.
SKIP_SECTIONS = frozenset(
    {
        "references",
        "external links",
        "see also",
        "notes",
        "footnotes",
        "citations",
        "sources",
        "bibliography",
        "further reading",
        "works cited",
        "explanatory notes",
    }
)

# Wrapper elements that hold page furniture rather than article prose.
SKIP_CLASSES = frozenset(
    {
        "navbox",
        "navbox-styles",
        "vertical-navbox",
        "sidebar",
        "infobox",
        "metadata",
        "ambox",
        "mbox",
        "thumb",
        "tright",
        "tleft",
        "reflist",
        "refbegin",
        "refend",
        "mw-references-wrap",
        "hatnote",
        "dablink",
        "printfooter",
        "catlinks",
        "shortdescription",
        "mw-empty-elt",
        "mw-authority-control",
        "gallery",
        "quotebox",
        "toc",
        "toccolours",
        "noprint",
        "mw-editsection",
    }
)

NOISE_TABLE_CLASSES = frozenset(
    {
        "navbox",
        "navbox-styles",
        "vertical-navbox",
        "sidebar",
        "infobox",
        "metadata",
        "ambox",
        "toccolours",
        "mw-collapsible",
        "wikitable-striped",
    }
)

CONTAINER_TAGS = frozenset({"section", "div"})
MAX_WRAPPER_DEPTH = 4

INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
CITATION_PATTERN = re.compile(
    r"\[\s*(?:\d+|[a-z]|note\s+\d+|citation needed|clarification needed|edit)\s*\]",
    re.IGNORECASE,
)
WHITESPACE_PATTERN = re.compile(r"[ \t\r\n\u00a0\u200b]+")
MEDIA_ARTIFACT_PATTERN = re.compile(r"\s*(?:ⓘ|\(\s*listen\s*\)|play\s*media)\s*", re.IGNORECASE)
CJK_GAP_PATTERN = re.compile(
    r"(?<=[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af])\s+(?=[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af])"
)
DISAMBIGUATION_HINT = re.compile(
    r"\b(?:may|might|can|most commonly|most often|usually|often)\s+refers?\s+to\b"
    r"|\bdisambiguation\s+page\b",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------
# The UI layer never sees these directly: they are converted into
# self.error inside WikipediaExtractor.__init__.


class ExtractorError(Exception):
    """Base class for every recoverable failure."""


class ArticleNotFound(ExtractorError):
    """The topic does not map to a usable article."""


class NetworkError(ExtractorError):
    """Wikipedia could not be reached, or answered with an HTTP error."""


class ParseError(ExtractorError):
    """The page was fetched but its structure could not be understood."""


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------


@dataclass
class Section:
    """One top-level (h2) section, stored as data rather than HTML."""

    index: int
    title: str
    blocks: List[Tuple[str, object]] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.blocks


# --------------------------------------------------------------------------
# Pure helpers (no I/O, no network, easy to unit-test)
# --------------------------------------------------------------------------


def to_title(topic: str) -> str:
    """Normalise what the user typed into a Wikipedia-ready title.

    '  python (programming language) ' -> 'Python (programming language)'
    """
    text = (topic or "").replace("_", " ").strip()
    text = WHITESPACE_PATTERN.sub(" ", text)
    if not text:
        return ""
    # Wikipedia titles are case-sensitive after the first letter, but users
    # almost always type lower case. Capitalise only the very first character.
    return text[0].upper() + text[1:]


def safe_filename(title: str) -> str:
    """Article title -> a file name that is legal on Windows, macOS and Linux."""
    name = INVALID_FILENAME_CHARS.sub("_", title or "wikipedia")
    name = WHITESPACE_PATTERN.sub("_", name).strip("._ ")
    name = name[:80] or "wikipedia"
    return "%s_extract.txt" % name


def numbered_filename(filename: str, existing_names: Sequence[str]) -> str:
    """Return a name that does not collide with anything already downloaded.

    'Singapore_extract.txt' + ['Singapore_extract.txt']
        -> 'Singapore_extract (2).txt'
    """
    existing = set(existing_names or [])
    if filename not in existing:
        return filename

    if filename.lower().endswith(".txt"):
        stem, extension = filename[:-4], ".txt"
    else:
        stem, extension = filename, ""

    counter = 2
    candidate = "%s (%d)%s" % (stem, counter, extension)
    while candidate in existing:
        counter += 1
        candidate = "%s (%d)%s" % (stem, counter, extension)
    return candidate


def format_txt(article_title: str, article_url: str, sections: Sequence[str]) -> str:
    """Build the complete .txt content.

    Takes the already-rendered section strings and wraps them in a header.
    Returns a string - it deliberately does not write anything to disk,
    because on Streamlit Community Cloud the container filesystem is
    ephemeral and the user cannot reach it.
    """
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines: List[str] = [
        "=" * 78,
        "WIKIPEDIA ARTICLE EXTRACT",
        "=" * 78,
        "Article    : %s" % article_title,
        "Source URL : %s" % article_url,
        "Extracted  : %s" % stamp,
        "Sections   : %d selected" % len(sections),
        "Licence    : Wikipedia content is licensed under %s." % LICENCE_NAME,
        "             %s" % LICENCE_URL,
        "=" * 78,
        "",
    ]
    for block in sections:
        lines.append(block)
        lines.append("")
        lines.append("")

    lines.append("=" * 78)
    lines.append("End of extract. %d section(s), generated by the Wikipedia Extractor web app." % len(sections))
    lines.append("=" * 78)
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Scraping layer
# --------------------------------------------------------------------------


class WikipediaExtractor:
    """Fetches one article and exposes its sections.

    The constructor does the work, so the UI only has to check `.error`
    afterwards:

        extractor = WikipediaExtractor("Singapore")
        if extractor.error:
            st.error(extractor.error)
    """

    def __init__(self, topic: str, timeout: Optional[int] = None) -> None:
        # Initialise every attribute first: the UI reads them even when the
        # fetch below fails, and a missing attribute would raise AttributeError.
        self.topic = (topic or "").strip()
        self.title = ""
        self.url = ""
        self.sections: List[Section] = []
        self.error: Optional[str] = None

        self.timeout = timeout or TIMEOUT_SECONDS
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

        try:
            self._load()
        except ExtractorError as error:
            self.error = str(error)
        except Exception as error:  # noqa: BLE001 - the UI must never crash
            self.error = "Unexpected error while reading Wikipedia: %s" % error

    # -- public API --------------------------------------------------------

    def section_titles(self) -> List[str]:
        """The options for the multiselect widget."""
        return [section.title for section in self.sections]

    def extract(self, title: str) -> str:
        """Render one section as plain text, ready for the preview and file."""
        for section in self.sections:
            if section.title == title:
                return self._render_section(section)
        raise KeyError("No section titled %r" % title)

    # -- loading -----------------------------------------------------------

    def _load(self) -> None:
        display = to_title(self.topic)
        if not display:
            raise ArticleNotFound("Please type a topic - for example: Singapore")

        url = WIKI_BASE + quote(display.replace(" ", "_"), safe="/")
        response = self._get(url)

        if response.status_code == 404:
            self._load_via_search(display)
            return
        if response.status_code != 200:
            raise NetworkError(
                "Wikipedia returned HTTP %d for '%s'. Please try again later."
                % (response.status_code, display)
            )
        self._parse_response(response)

    def _get(self, url: str, params: Optional[Dict[str, object]] = None) -> requests.Response:
        last_error: Optional[Exception] = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return self.session.get(url, params=params, timeout=self.timeout)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as error:
                last_error = error
                if attempt < MAX_ATTEMPTS:
                    time.sleep(1.5)
            except requests.exceptions.RequestException as error:
                raise NetworkError("Could not fetch the page: %s" % error) from error
        raise NetworkError(
            "Could not reach Wikipedia after %d attempts. Check the connection and try again."
            % MAX_ATTEMPTS
        ) from last_error

    def _load_via_search(self, display: str) -> None:
        params = {"search": display, "title": "Special:Search", "fulltext": 1}
        response = self._get(SEARCH_URL, params=params)
        if response.status_code != 200:
            raise NetworkError("Search for '%s' failed with HTTP %d." % (display, response.status_code))

        soup = BeautifulSoup(response.text, "html.parser")
        results = soup.find("div", class_="mw-search-results")
        if results is None:
            raise ArticleNotFound(
                "No Wikipedia article found for '%s'. Check the spelling and try a "
                "more common name." % display
            )

        heading = results.find("div", class_="mw-search-result-heading")
        link = heading.find("a") if heading is not None else None
        if link is None or not link.get("href"):
            raise ArticleNotFound(
                "No Wikipedia article found for '%s'. Check the spelling and try a "
                "more common name." % display
            )

        follow_up = self._get(urljoin(SEARCH_URL, link["href"]))
        if follow_up.status_code != 200:
            raise NetworkError("The suggested article could not be opened (HTTP %d)." % follow_up.status_code)
        self._parse_response(follow_up)

    # -- parsing -----------------------------------------------------------

    def _parse_response(self, response: requests.Response) -> None:
        soup = BeautifulSoup(response.text, "html.parser")

        # Scope the search to the article body: the page header contains small
        # mw-parser-output widgets (coordinates, icons) that would otherwise win.
        container = soup.find("div", id="mw-content-text")
        content = container.find("div", class_="mw-parser-output") if container else None
        if content is None:
            content = soup.find("div", class_="mw-parser-output")
        if content is None:
            raise ParseError(
                "The page was downloaded but its content could not be located. "
                "Wikipedia's HTML may have changed."
            )

        heading = soup.find("h1", id="firstHeading")
        self.title = _clean(heading.get_text("")) if heading else self.url

        _prune(content)

        suggestions = _disambiguation_suggestions(content, self.title)
        if suggestions is not None:
            message = "'%s' is a disambiguation page, not an article." % self.title
            if suggestions:
                message += " Try one of these instead: %s" % ", ".join(suggestions)
            else:
                message += " Try a more specific name."
            raise ArticleNotFound(message)

        sections = _collect_sections(content)
        if not sections:
            raise ParseError("'%s' has no readable top-level sections. Try another article." % self.title)

        self.sections = sections
        self.url = response.url

    # -- rendering ---------------------------------------------------------

    def _render_section(self, section: Section) -> str:
        lines: List[str] = [
            "-" * 78,
            "SECTION %d: %s" % (section.index, section.title.upper()),
            "-" * 78,
            "",
        ]
        table_number = 0

        for kind, payload in section.blocks:
            if kind == "heading":
                level, text = payload  # type: ignore[misc]
                prefix = "  " * max(1, level - 2)
                lines.append("")
                lines.append("%s%s" % (prefix, text))
                lines.append("%s%s" % (prefix, "~" * min(len(text), 60)))
            elif kind == "paragraph":
                lines.append(_wrap(str(payload), 88))
                lines.append("")
            elif kind == "list":
                ordered, items = payload  # type: ignore[misc]
                for position, item in enumerate(items, start=1):
                    bullet = ("%d." % position) if ordered else "-"
                    lines.append(
                        _wrap(str(item), 84, initial="  %-3s " % bullet, subsequent="      ")
                    )
                lines.append("")
            elif kind == "table":
                table_number += 1
                rows, has_header = payload  # type: ignore[misc]
                lines.extend(_render_table(rows, has_header, table_number))
                lines.append("")

        while lines and lines[-1] == "":
            lines.pop()
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Internal helpers
# --------------------------------------------------------------------------


def _wrap(text: str, width: int, initial: str = "", subsequent: str = "") -> str:
    return textwrap.fill(text, width=width, initial_indent=initial, subsequent_indent=subsequent)


def _render_table(rows: Sequence[Sequence[str]], has_header: bool, number: int) -> List[str]:
    if not rows:
        return []
    columns = max(len(row) for row in rows)
    widths = []
    for column in range(columns):
        longest = max((len(row[column]) for row in rows if column < len(row)), default=0)
        widths.append(min(longest, 36))

    lines = ["  [Table %d]" % number]
    for position, row in enumerate(rows):
        cells = [row[c].ljust(widths[c]) if c < len(row) else "" for c in range(columns)]
        lines.append("  " + " | ".join(cells).rstrip())
        if position == 0 and has_header:
            lines.append("  " + "-+-".join("-" * max(w, 3) for w in widths))
    return lines


def _clean(text: str) -> str:
    if not text:
        return ""
    text = CITATION_PATTERN.sub("", text)
    text = MEDIA_ARTIFACT_PATTERN.sub(" ", text)
    text = WHITESPACE_PATTERN.sub(" ", text)
    text = CJK_GAP_PATTERN.sub("", text)
    return text.strip()


def _prune(content) -> None:
    for tag_name in ("style", "script", "noscript"):
        for element in content.find_all(tag_name):
            element.decompose()
    for classes in ("reference", "noprint"):
        for element in content.find_all("sup", class_=classes):
            element.decompose()
    for classes in ("mw-editsection", "mw-cite-backlink"):
        for element in content.find_all("span", class_=classes):
            element.decompose()


def _first_meaningful_paragraph(content) -> str:
    for paragraph in content.find_all("p"):
        text = WHITESPACE_PATTERN.sub(" ", paragraph.get_text("")).strip()
        if text:
            return text
    return ""


def _disambiguation_suggestions(content, title: str) -> Optional[List[str]]:
    opening = _first_meaningful_paragraph(content)
    if not opening or len(opening) >= 400 or not DISAMBIGUATION_HINT.search(opening):
        return None

    titles: List[str] = []
    for link in content.find_all("a", href=True):
        href = link["href"]
        position = href.find("/wiki/")
        if position == -1:
            continue
        slug = href[position + len("/wiki/"):].split("#")[0].split("?")[0]
        if not slug or ":" in slug:
            continue
        if slug.replace("_", " ").strip().lower() == title.strip().lower():
            continue
        candidate = _clean(link.get_text("")) or slug.replace("_", " ")
        if candidate and candidate not in titles:
            titles.append(candidate)
        if len(titles) >= 5:
            break
    return titles


def _collect_sections(content) -> List[Section]:
    sections: List[Section] = []
    current: Optional[Section] = None
    stray: List[Tuple[str, object]] = []

    for kind, payload in _iter_blocks(content):
        if kind == "heading":
            level, text = payload  # type: ignore[misc]
            if level == 2:
                current = Section(index=len(sections) + 1, title=text)
                sections.append(current)
            elif current is not None:
                current.blocks.append(("heading", (level, text)))
            continue

        if current is None:
            stray.append((kind, payload))
        else:
            current.blocks.append((kind, payload))

    if not sections and stray:
        sections.append(Section(index=1, title="Article overview", blocks=stray))

    kept: List[Section] = []
    for section in sections:
        if section.title.strip().lower() in SKIP_SECTIONS:
            continue
        if section.is_empty:
            continue
        section.index = len(kept) + 1
        kept.append(section)
    return kept


def _iter_blocks(content, depth: int = 0):
    """Yield ('heading'|'paragraph'|'list'|'table', payload) in document order.

    Wikipedia ships two shapes for article bodies: a flat one where headings
    are direct children, and a sectioned one where every section is wrapped
    in <section>. The walker descends through containers so both work.
    """
    if depth > MAX_WRAPPER_DEPTH:
        return

    for element in content.find_all(recursive=False):
        name = element.name
        classes = element.get("class") or []

        if name in ("style", "script", "noscript"):
            continue
        if SKIP_CLASSES.intersection(classes):
            continue

        if "mw-heading" in classes:
            heading = element.find(["h2", "h3", "h4"])
            if heading is not None:
                yield ("heading", (int(heading.name[1]), _clean(heading.get_text(""))))
            continue

        if name in ("h2", "h3", "h4"):
            yield ("heading", (int(name[1]), _clean(element.get_text(""))))
        elif name == "p":
            text = _clean(element.get_text(""))
            if text:
                yield ("paragraph", text)
        elif name in ("ul", "ol"):
            items = [_clean(item.get_text("")) for item in element.find_all("li", recursive=False)]
            items = [item for item in items if item]
            if items:
                yield ("list", (name == "ol", items))
        elif name == "table":
            if NOISE_TABLE_CLASSES.intersection(classes):
                continue
            rows, has_header = _table_rows(element)
            if len(rows) >= 2:
                yield ("table", (rows, has_header))
        elif name in CONTAINER_TAGS:
            for block in _iter_blocks(element, depth + 1):
                yield block


def _table_rows(table) -> Tuple[List[List[str]], bool]:
    rows: List[List[str]] = []
    has_header = False

    for row_index, row in enumerate(table.find_all("tr")):
        cells = row.find_all(["th", "td"])
        if not cells:
            continue
        values = [
            WHITESPACE_PATTERN.sub(" ", CITATION_PATTERN.sub("", cell.get_text(""))).strip()
            for cell in cells
        ]
        if not any(values):
            continue
        if row_index == 0 and row.find("th") is not None:
            has_header = True
        rows.append(values)

    if not rows:
        return [], has_header

    width = max(len(row) for row in rows)
    keep = [c for c in range(width) if any(c < len(row) and row[c] for row in rows)]
    rows = [[row[c] if c < len(row) else "" for c in keep] for row in rows]
    rows = [row for row in rows if any(row)]
    return rows, has_header
