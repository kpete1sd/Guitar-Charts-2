import hashlib
import re

import streamlit as st

from chartlib import (
    build_song, parse_chart, read_upload, render_docx, render_pdf, to_html, to_text,
)

st.set_page_config(page_title="Guitar Chart Formatter", page_icon="🎸", layout="wide")
st.markdown(
    "<style>textarea{font-family:ui-monospace,Menlo,Consolas,monospace !important;}</style>",
    unsafe_allow_html=True,
)

st.title("🎸 Guitar Chart Formatter")
st.caption(
    "Upload a chart in almost any text-based format, optionally paste the full lyrics, "
    "and get a clean, standard chart as a PDF or Word file."
)

st.session_state.setdefault("lyrics", "")

# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.header("Options")
    st.subheader("Song info (optional)")
    st.caption("Leave blank to use what's found in the chart.")
    o_title = st.text_input("Title")
    o_artist = st.text_input("Artist")
    o_key = st.text_input("Key")
    o_capo = st.text_input("Capo")
    st.subheader("Chords")
    transpose = st.slider("Transpose (semitones)", -11, 11, 0)
    flats = st.checkbox("Use flats (Bb, Eb…) instead of sharps", value=False)
    st.subheader("Lyrics")
    insert_missing = st.checkbox(
        "Insert pasted lines missing from the chart", value=True,
        help="Lines found in your pasted lyrics but not in the uploaded chart are added (without chords).",
    )
    st.subheader("Output")
    font_size = st.slider("Font size (pt)", 8, 24, 14,
                          help="Long lines are shrunk automatically so chords stay aligned.")
    one_page = st.checkbox(
        "Fit the whole song on one page", value=False,
        help="Shrinks the font as needed (down to 6 pt) so there are no page turns. "
             "Otherwise the song flows onto extra pages at the size above.",
    )

# ------------------------------------------------------------------ inputs
left, right = st.columns(2, gap="large")

with left:
    st.subheader("1 · Upload a chart")
    up = st.file_uploader(
        "Chart file",
        type=["txt", "cho", "chordpro", "chopro", "pro", "crd", "md", "docx", "pdf"],
        help="Chords-above-lyrics text, ChordPro / [C]inline chords, Word, or text-based PDF.",
    )

with right:
    st.subheader("2 · Paste full lyrics (optional)")
    with st.form("lyrics_form"):
        pasted = st.text_area(
            "Lyrics",
            value=st.session_state["lyrics"],
            height=200,
            placeholder="Paste the complete lyrics here.\nSection labels like [Verse 1] or [Chorus] are optional.",
        )
        b1, b2 = st.columns(2)
        use = b1.form_submit_button("Use these lyrics", type="primary")
        clear = b2.form_submit_button("Clear")
    if use:
        st.session_state["lyrics"] = pasted
    if clear:
        st.session_state["lyrics"] = ""
        st.rerun()
    if st.session_state["lyrics"].strip():
        st.caption(f"✅ Lyrics loaded ({len(st.session_state['lyrics'].splitlines())} lines)")

chart_text = ""
if up is not None:
    try:
        chart_text = read_upload(up.name, up.getvalue())
    except Exception as exc:  # noqa: BLE001
        st.error(f"Couldn't read **{up.name}**: {exc}")

lyrics_text = st.session_state["lyrics"]
if not chart_text.strip() and not lyrics_text.strip():
    st.info("Upload a chart and/or paste lyrics to get started.")
    st.stop()

# ------------------------------------------------------------------ build
song, notes = build_song(
    chart_text, lyrics_text, transpose, flats,
    {"title": o_title, "artist": o_artist, "key": o_key, "capo": o_capo},
    insert_missing,
)
base_text = to_text(song)
sig = hashlib.md5(base_text.encode()).hexdigest()[:10]

st.divider()
for n in notes:
    st.caption(f"ℹ️ {n}")

ed, pv = st.columns(2, gap="large")
with ed:
    st.subheader("3 · Review & edit")
    st.caption(
        "Chords sit on the line above the lyrics they belong to. `[Verse 1]`-style lines start a section. "
        "Edits update the preview and downloads. Changing the inputs or sidebar options resets this box."
    )
    edited = st.text_area("Standard chart", value=base_text, height=560, key=f"edit_{sig}",
                          label_visibility="collapsed")
final = parse_chart(edited)

with pv:
    st.subheader("Preview")
    st.html(to_html(final))

# ------------------------------------------------------------------ downloads
st.divider()
st.subheader("4 · Download")
safe = re.sub(r"[^A-Za-z0-9_-]+", "_", f"{final.title or 'chart'}{'-' + final.artist if final.artist else ''}").strip("_")
d1, d2, _ = st.columns([1, 1, 2])
try:
    d1.download_button("⬇️ PDF", render_pdf(final, font_size, one_page), file_name=f"{safe}.pdf",
                       mime="application/pdf", type="primary", use_container_width=True)
    d2.download_button(
        "⬇️ Word (.docx)", render_docx(final, font_size, one_page), file_name=f"{safe}.docx",
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        use_container_width=True,
    )
except Exception as exc:  # noqa: BLE001
    st.error(f"Couldn't build the files: {exc}")
