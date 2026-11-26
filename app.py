"""
app.py - Streamlit UI for the Wikipedia Article Extractor
=========================================================

ISOM3400 Lab 12. This file is the UI layer only. All scraping lives in
wikipedia_extractor_core.py, which knows nothing about Streamlit.

The four stages, driven by st.session_state because every widget
interaction re-runs the whole script:

    Search  -> session_state.article + session_state.titles
    Select  -> session_state.selected
    Extract -> session_state.sections
    Download-> session_state.downloaded_filenames

Nothing is ever written to disk on the server: the download button hands
the file content straight to the browser.

Author: Mo Yeuk Hei (21232498)
"""

import streamlit as st

# set_page_config must be the first Streamlit call in the script.
st.set_page_config(page_title="Wikipedia Extractor", page_icon="📚", layout="wide")

from wikipedia_extractor_core import (  # noqa: E402  (import after config on purpose)
    WikipediaExtractor,
    format_txt,
    numbered_filename,
    safe_filename,
)

DEFAULTS = {
    "topic": "",
    "article": None,
    "titles": [],
    "selected": [],
    "sections": [],
    "downloaded_filenames": [],
}


def reset_state() -> None:
    """Put every key back to its default. Used by Start Over and the guard."""
    st.session_state.topic = ""
    st.session_state.article = None
    st.session_state.titles = []
    st.session_state.selected = []
    st.session_state.sections = []
    st.session_state.downloaded_filenames = []


def remember_download(name: str) -> None:
    """on_click callback: record that this file name has been handed out."""
    st.session_state.downloaded_filenames.append(name)


# --------------------------------------------------------------------------
# Session state: create the keys once, never before they are read
# --------------------------------------------------------------------------

for key, value in DEFAULTS.items():
    if key not in st.session_state:
        st.session_state[key] = value.copy() if isinstance(value, list) else value

# Guard against a half-broken state (e.g. the browser kept a tab open across a
# restart): an extract in memory but no article behind it can only crash.
if st.session_state.article is None and (st.session_state.titles or st.session_state.sections):
    reset_state()
    st.rerun()


# --------------------------------------------------------------------------
# Sidebar
# --------------------------------------------------------------------------

with st.sidebar:
    st.header("Wikipedia Extractor")
    st.caption("ISOM3400 Lab 12 - Mo Yeuk Hei (21232498)")
    if st.button("Start over"):
        for key in list(st.session_state.keys()):
            del st.session_state[key]
        st.rerun()
    st.divider()
    st.caption(
        "Fetches an article from Wikipedia, extracts the sections you pick, "
        "and hands you a clean .txt file."
    )


# --------------------------------------------------------------------------
# Header + progress indicator
# --------------------------------------------------------------------------

st.title("Wikipedia Article Extractor")

with st.expander("How to use this app"):
    st.markdown(
        "1. **Enter a topic** in the box below and press **Search**.\n"
        "2. **Select one or more sections** from the list.\n"
        "3. Press **Extract** to build the text.\n"
        "4. Press **Download .txt** to save it to your device."
    )

progress = st.columns(4)
progress[0].caption(("✅ " if st.session_state.topic else "⬜ ") + "Search")
progress[1].caption(("✅ " if st.session_state.titles else "⬜ ") + "Select")
progress[2].caption(("✅ " if st.session_state.sections else "⬜ ") + "Extract")
progress[3].caption(("✅ " if st.session_state.downloaded_filenames else "⬜ ") + "Download")

st.divider()


# --------------------------------------------------------------------------
# Stage 1 - Search
# --------------------------------------------------------------------------

with st.form("search_form"):
    typed = st.text_input(
        "Enter a topic",
        value=st.session_state.topic,
        placeholder="e.g. Singapore, Taylor Swift, Python (programming language)",
    )
    submitted = st.form_submit_button("Search")

if submitted:
    topic = typed.strip()
    if not topic:
        st.warning("Please enter a topic first - for example: Singapore")
    else:
        with st.spinner("Fetching from Wikipedia..."):
            article = WikipediaExtractor(topic)
        st.session_state.article = article
        st.session_state.topic = topic
        # A new search always invalidates anything extracted from the old one.
        st.session_state.selected = []
        st.session_state.sections = []
        st.session_state.titles = [] if article.error else article.section_titles()


# --------------------------------------------------------------------------
# Stages 2-4 - rendered from state on every rerun
# --------------------------------------------------------------------------

article = st.session_state.article

if article is not None:
    if article.error:
        st.error(article.error)

    else:
        st.success("Found: **%s**" % article.title)
        st.markdown("Source: [%s](%s)" % (article.url, article.url))

        titles = st.session_state.titles

        if not titles:
            st.warning(
                "This article has no usable top-level sections. Try a different topic."
            )
        else:
            if len(titles) == 1:
                st.warning("This article only has one usable section, so it was selected for you.")
                st.session_state.selected = list(titles)
            else:
                new_selection = st.multiselect(
                    "Select the sections to extract",
                    options=titles,
                    default=st.session_state.selected,
                )
                # Changing the selection makes any existing preview stale, so
                # drop it rather than letting the user download the wrong text.
                if new_selection != st.session_state.selected:
                    st.session_state.sections = []
                st.session_state.selected = new_selection

            # Stage 3 - Extract
            if st.button("Extract", type="primary"):
                if not st.session_state.selected:
                    st.warning("Select at least one section first.")
                else:
                    with st.spinner("Extracting..."):
                        st.session_state.sections = [
                            article.extract(title) for title in st.session_state.selected
                        ]

            # Stage 4 - Preview + Download
            if st.session_state.sections:
                content = format_txt(
                    article.title, article.url, st.session_state.sections
                )

                st.subheader("Preview")
                with st.container(border=True):
                    st.text(content)

                base_name = safe_filename(article.title)
                file_name = numbered_filename(base_name, st.session_state.downloaded_filenames)

                if file_name != base_name:
                    st.warning(
                        "You have already downloaded this extract in this session. "
                        "This one will be saved as **%s** so nothing gets overwritten."
                        % file_name
                    )

                st.download_button(
                    label="Download .txt",
                    data=content,
                    file_name=file_name,
                    mime="text/plain",
                    on_click=remember_download,
                    args=(file_name,),
                )

st.divider()
st.caption("Content from Wikipedia, CC BY-SA 4.0.")

# Debug view, collapsed so it does not clutter the page.
with st.expander("Debug: session state"):
    st.write(st.session_state)
