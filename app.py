import hashlib
import io
import re
import zipfile

import streamlit as st

from chartlib import (
    build_song, is_youtube_url, parse_chart, read_upload, render_docx, render_pdf,
    render_pdf_many, to_html, to_text,
)

MAX_SONGS = 10

st.set_page_config(page_title="Guitar Chart Formatter", page_icon="🎸", layout="wide")
st.markdown(
    "<style>textarea{font-family:ui-monospace,Menlo,Consolas,monospace !important;}</style>",
    unsafe_allow_html=True,
)


# ------------------------------------------------------------------ cached renderers
@st.cache_data(show_spinner=False, max_entries=64)
def pdf_bytes(chart_text, font_size, one_page, url):
    return render_pdf(parse_chart(chart_text), font_size, one_page, url)


@st.cache_data(show_spinner=False, max_entries=64)
def docx_bytes(chart_text, font_size, one_page):
    return render_docx(parse_chart(chart_text), font_size, one_page)


@st.cache_data(show_spinner=False, max_entries=16)
def combined_pdf_bytes(items, font_size, one_page):
    return render_pdf_many([(parse_chart(t), u) for t, u in items], font_size, one_page)


def safe_name(song):
    raw = f"{song.title or 'chart'}{'-' + song.artist if song.artist else ''}"
    return re.sub(r"[^A-Za-z0-9_-]+", "_", raw).strip("_") or "chart"


def zip_bytes(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files:
            z.writestr(name, data)
    return buf.getvalue()


st.title("🎸 Guitar Chart Formatter")
st.caption(
    f"Upload up to {MAX_SONGS} charts in almost any text-based format, optionally paste full lyrics for each, "
    "and get clean, standard charts as PDF or Word files. Each PDF ends with a YouTube link."
)

# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.header("Options for all songs")
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
        "Fit each song on one page", value=False,
        help="Shrinks the font as needed (down to 6 pt) so there are no page turns. "
             "Otherwise songs flow onto extra pages at the size above.",
    )

# ------------------------------------------------------------------ inputs
st.subheader("1 · Add your songs")
ups = st.file_uploader(
    f"Chart files (up to {MAX_SONGS})",
    type=["txt", "cho", "chordpro", "chopro", "pro", "crd", "md", "docx", "pdf"],
    accept_multiple_files=True,
    help="Chords-above-lyrics text, ChordPro / [C]inline chords, Word, or PDF.",
)
with st.expander("Or paste a chart / lyrics instead of uploading a file"):
    pasted_chart = st.text_area(
        "Pasted song", height=180, label_visibility="collapsed",
        placeholder="Paste a chart (chords above lyrics) or just lyrics here. It counts as one of your songs.",
    )

songs = []  # {"id", "label", "text"}
errors = []
for i, up in enumerate(ups or []):
    try:
        songs.append({"id": f"f{i}_{hashlib.md5(up.getvalue()).hexdigest()[:8]}", "label": up.name,
                      "text": read_upload(up.name, up.getvalue())})
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Couldn't read **{up.name}**: {exc}")
if pasted_chart.strip():
    songs.append({"id": "pasted", "label": "Pasted song", "text": pasted_chart})
for e in errors:
    st.error(e)
if len(songs) > MAX_SONGS:
    st.warning(f"You added {len(songs)} songs; only the first {MAX_SONGS} are used. Remove the extras to continue.")
    songs = songs[:MAX_SONGS]
if not songs:
    st.info("Upload one or more charts (or paste one) to get started.")
    st.stop()

# ------------------------------------------------------------------ per-song
st.divider()
st.subheader(f"2 · Review each song ({len(songs)})")
st.caption(
    "Open a song to add lyrics, a YouTube link, or edit the chart text. "
    "Changing the song's inputs or the sidebar options resets that song's edit box."
)

results = []  # {"song","text","url","include","name"}
for n, sg in enumerate(songs):
    k = sg["id"]
    with st.expander(f"{n + 1}. {sg['label']}", expanded=(len(songs) == 1)):
        c1, c2, c3, c4 = st.columns(4)
        o_title = c1.text_input("Title", key=f"title_{k}", help="Leave blank to use what's found in the chart.")
        o_artist = c2.text_input("Artist", key=f"artist_{k}")
        o_key = c3.text_input("Key", key=f"key_{k}")
        o_capo = c4.text_input("Capo", key=f"capo_{k}")

        yt = st.text_input(
            "YouTube link for this song", key=f"yt_{k}", placeholder="https://www.youtube.com/watch?v=…",
            help="Paste the video's address. Leave blank and the PDF will link to a YouTube search for the song instead.",
        )
        if yt.strip() and not is_youtube_url(yt):
            st.warning("That doesn't look like a YouTube address. It will still be used as written.")

        lyr = st.text_area(
            "Full lyrics (optional)", key=f"lyr_{k}", height=130,
            placeholder="Paste the complete lyrics. Click outside the box to apply. "
                        "Section labels like [Verse 1] are optional.",
        )

        song, notes = build_song(
            sg["text"], lyr, transpose, flats,
            {"title": o_title, "artist": o_artist, "key": o_key, "capo": o_capo}, insert_missing,
        )
        base_text = to_text(song)
        sig = hashlib.md5(base_text.encode()).hexdigest()[:10]
        for msg in notes:
            st.caption(f"ℹ️ {msg}")

        ed, pv = st.columns(2, gap="large")
        with ed:
            edited = st.text_area(
                "Standard chart", value=base_text, height=420, key=f"edit_{k}_{sig}",
                help="Chords sit on the line above the lyrics they belong to. "
                     "A line like [Verse 1] starts a section.",
            )
        final = parse_chart(edited)
        with pv:
            st.html(to_html(final))

        include = st.checkbox("Include in combined PDF", value=True, key=f"inc_{k}")
        name = safe_name(final)
        d1, d2, _ = st.columns([1, 1, 2])
        try:
            d1.download_button("⬇️ PDF", pdf_bytes(edited, font_size, one_page, yt), file_name=f"{name}.pdf",
                               mime="application/pdf", key=f"dlp_{k}")
            d2.download_button(
                "⬇️ Word (.docx)", docx_bytes(edited, font_size, one_page), file_name=f"{name}.docx",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                key=f"dlw_{k}")
        except Exception as exc:  # noqa: BLE001
            st.error(f"Couldn't build the files: {exc}")
    results.append({"text": edited, "url": yt, "include": include, "name": name})

# ------------------------------------------------------------------ batch downloads
st.divider()
st.subheader("3 · Download everything")
chosen = [r for r in results if r["include"]]
try:
    b1, b2, b3 = st.columns(3)
    if chosen:
        b1.download_button(
            f"⬇️ Combined PDF ({len(chosen)} song{'s' if len(chosen) != 1 else ''})",
            combined_pdf_bytes(tuple((r["text"], r["url"]) for r in chosen), font_size, one_page),
            file_name="song_charts.pdf", mime="application/pdf", type="primary",
            help="One PDF, each song starting on a new page, in the order above.",
        )
    else:
        b1.caption("Tick “Include in combined PDF” on at least one song.")
    prefix = lambda i: f"{i + 1:02d}_"  # noqa: E731
    b2.download_button(
        "⬇️ ZIP of PDFs",
        zip_bytes([(f"{prefix(i)}{r['name']}.pdf", pdf_bytes(r["text"], font_size, one_page, r["url"]))
                   for i, r in enumerate(results)]),
        file_name="song_charts_pdf.zip", mime="application/zip",
    )
    b3.download_button(
        "⬇️ ZIP of Word files",
        zip_bytes([(f"{prefix(i)}{r['name']}.docx", docx_bytes(r["text"], font_size, one_page))
                   for i, r in enumerate(results)]),
        file_name="song_charts_word.zip", mime="application/zip",
    )
except Exception as exc:  # noqa: BLE001
    st.error(f"Couldn't build the batch files: {exc}")
