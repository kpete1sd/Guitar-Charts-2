"""chartlib: parse guitar charts in several formats and render a standard chart.

Supported inputs
  * chords-above-lyrics plain text (Ultimate-Guitar style, .txt, PDF/DOCX text)
  * ChordPro / inline brackets:  "Hel[C]lo wor[G]ld"  and {directives}
  * chord-only lines, section headers, simple ASCII tab blocks

Outputs
  * standard text (re-parseable, used for the editable box)
  * HTML preview, PDF (reportlab), Word .docx (python-docx)
"""
from __future__ import annotations

import html
import io
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass
class Line:
    lyrics: str = ""
    chords: list = field(default_factory=list)  # [(column, chord)]
    kind: str = "lyric"  # lyric | chords | tab | blank


@dataclass
class Section:
    name: str = ""
    lines: list = field(default_factory=list)


@dataclass
class Song:
    title: str = ""
    artist: str = ""
    key: str = ""
    capo: str = ""
    extra: dict = field(default_factory=dict)
    sections: list = field(default_factory=list)

    def has_chords(self) -> bool:
        return any(l.chords for s in self.sections for l in s.lines)


# --------------------------------------------------------------------------- #
# Chord helpers
# --------------------------------------------------------------------------- #

NOTE_SHARP = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
NOTE_FLAT = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]
NOTE_IDX = {
    "C": 0, "C#": 1, "Db": 1, "D": 2, "D#": 3, "Eb": 3, "E": 4, "Fb": 4,
    "E#": 5, "F": 5, "F#": 6, "Gb": 6, "G": 7, "G#": 8, "Ab": 8, "A": 9,
    "A#": 10, "Bb": 10, "B": 11, "Cb": 11, "B#": 0,
}

CHORD_RE = re.compile(
    r"^[A-G][#b]?"
    r"(?:maj|min|dim|aug|sus|add|m|M|\+|°|ø|-)?"
    r"(?:\d{1,2})?"
    r"(?:(?:sus|add|maj|alt|b|#|\+|-)\d{0,2})*"
    r"(?:\([^)]*\))?"
    r"(?:/[A-G][#b]?)?$"
)
SKIP_TOKEN_RE = re.compile(
    r"^(\|+|\|?:\|*|:\|+|-+|/+|%|\.\.\.|\(?x\d+\)?|\(?\d+x\)?|\|\|:?)$", re.I
)


def _unparen(tok: str) -> str:
    if tok.startswith("(") and tok.endswith(")") and tok.count("(") == 1:
        return tok[1:-1]
    return tok


def is_chord(tok: str) -> bool:
    tok = _unparen(tok)
    if tok.upper().replace(".", "") == "NC":
        return True
    return bool(CHORD_RE.match(tok))


def is_chord_line(line: str) -> bool:
    toks = line.split()
    if not toks:
        return False
    n_chords = 0
    for t in toks:
        if is_chord(t):
            n_chords += 1
        elif not SKIP_TOKEN_RE.match(t):
            return False
    return n_chords >= 1


def chord_positions(line: str) -> list:
    return [(m.start(), _unparen(m.group())) for m in re.finditer(r"\S+", line) if is_chord(m.group())]


def transpose_chord(ch: str, n: int, flats: bool) -> str:
    if ch.upper().replace(".", "") == "NC":
        return ch
    names = NOTE_FLAT if flats else NOTE_SHARP

    def shift(note):
        return names[(NOTE_IDX[note] + n) % 12]

    m = re.match(r"^([A-G][#b]?)(.*)$", ch)
    if not m:
        return ch
    root, rest = m.groups()
    rest = re.sub(r"/([A-G][#b]?)", lambda mm: "/" + shift(mm.group(1)), rest)
    return shift(root) + rest


def transpose_song(song: Song, n: int, flats: bool) -> Song:
    if n % 12 == 0 and not flats:
        return song
    for sec in song.sections:
        for l in sec.lines:
            l.chords = [(c, transpose_chord(ch, n, flats)) for c, ch in l.chords]
    km = re.match(r"^([A-G][#b]?)(.*)$", song.key.strip()) if song.key else None
    if km and km.group(1) in NOTE_IDX:
        song.key = transpose_chord(km.group(1), n, flats) + km.group(2)
    return song


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

_SEC_WORDS = (
    r"(?:intro|verse|chorus|pre[- ]?chorus|post[- ]?chorus|bridge|outro|interlude|"
    r"instrumental|solo|tag|refrain|hook|coda|turnaround|ending|break|vamp|breakdown)"
)
HEADER_RE = re.compile(
    rf"^\s*[\[\(\{{<]?\s*{_SEC_WORDS}\s*(?:\d+|[A-C])?\s*(?:[-–:]\s*[\w .]+)?\s*[\]\)\}}>]?\s*:?\s*$",
    re.I,
)
DIRECTIVE_RE = re.compile(r"^\{\s*([A-Za-z_]+)\s*(?::\s*(.*?))?\s*\}$")
META_RE = re.compile(
    r"^(title|song|artist|by|key|capo|tempo|bpm|time|tuning|album|written by)\s*[:=]\s*(.+)$", re.I
)
LATE_META = {"key", "capo", "tempo", "bpm", "time", "tuning"}
TAB_RE = re.compile(r"^\s*[eEBGDAdg]\|[-0-9hpbrxsS/\\~()|\s^<>.*]{5,}$")
INLINE_RE = re.compile(r"\[([^\]\s]+)\]")


def header_name(s: str):
    if not HEADER_RE.match(s):
        return None
    name = re.sub(r"[\[\](){}<>:]", "", s)
    name = re.sub(r"\s+", " ", name).strip().title()
    return re.sub(r"\bX(\d)", r"x\1", name)


def _parse_inline(line: str):
    """'Hel[C]lo [G]world' -> ('Hello world', [(3,'C'),(6,'G')]) or None."""
    matches = [m for m in INLINE_RE.finditer(line) if is_chord(m.group(1))]
    if not matches:
        return None
    out, chords, pos = "", [], 0
    for m in matches:
        out += line[pos:m.start()]
        chords.append((len(out), _unparen(m.group(1))))
        pos = m.end()
    out += line[pos:]
    indent = len(out) - len(out.lstrip())
    chords = [(max(0, c - indent), ch) for c, ch in chords]
    return out.strip(), chords


def parse_chart(text: str, detect_chords: bool = True) -> Song:
    text = (
        text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
        .replace("\u00a0", " ").replace("♯", "#").replace("♭", "b")
    )
    raw = text.split("\n")
    n = len(raw)
    song = Song()
    state = {"cur": None, "started": False}

    def new_section(name=""):
        sec = Section(name)
        song.sections.append(sec)
        state["cur"] = sec

    def add(line: Line):
        if state["cur"] is None:
            new_section("")
        state["cur"].lines.append(line)
        state["started"] = True

    def add_blank():
        cur = state["cur"]
        if cur and cur.lines and cur.lines[-1].kind != "blank":
            cur.lines.append(Line(kind="blank"))

    def looks_structural(nxt: str) -> bool:
        s = nxt.strip()
        return (
            not s or header_name(s) is not None or bool(META_RE.match(s))
            or bool(DIRECTIVE_RE.match(s)) or bool(TAB_RE.match(nxt))
            or (detect_chords and (is_chord_line(nxt) or _parse_inline(nxt) is not None))
        )

    i = 0
    while i < n:
        ln = raw[i].rstrip()
        s = ln.strip()
        if not s:
            add_blank()
            i += 1
            continue

        d = DIRECTIVE_RE.match(s)
        if d:
            key, val = d.group(1).lower(), (d.group(2) or "").strip()
            if key in ("title", "t"):
                song.title = val
            elif key in ("subtitle", "st", "artist", "a"):
                song.artist = song.artist or val
            elif key == "key":
                song.key = val
            elif key == "capo":
                song.capo = val
            elif key.startswith("start_of_") or key in ("sov", "soc", "sob"):
                base = {"sov": "verse", "soc": "chorus", "sob": "bridge"}.get(key, key[9:])
                new_section(val or base.title())
            elif key.startswith("end_of_") or key in ("eov", "eoc", "eob"):
                add_blank()
            elif key in ("comment", "c", "ci", "cb", "comment_italic", "comment_box"):
                h = header_name(val)
                if h:
                    new_section(h)
                else:
                    add(Line(lyrics=val))
            i += 1
            continue

        m = META_RE.match(s)
        if m and (not state["started"] or m.group(1).lower() in LATE_META):
            k, v = m.group(1).lower(), m.group(2).strip()
            cm = re.search(r"\s{2,}capo\s*[:=]?\s*(\S+)\s*$", v, re.I) if k == "key" else None
            if cm:  # "Key: A     Capo: 2" on one line
                song.capo, v = cm.group(1), v[: cm.start()].strip()
            if k in ("title", "song"):
                song.title = v
            elif k in ("artist", "by", "written by"):
                song.artist = v
            elif k == "key":
                song.key = v
            elif k == "capo":
                song.capo = v
            else:
                song.extra[k] = v
            i += 1
            continue

        hdr = header_name(s)
        if hdr is not None:
            new_section(hdr)
            state["started"] = True
            i += 1
            continue

        if TAB_RE.match(ln):
            add(Line(lyrics=ln.rstrip(), kind="tab"))
            i += 1
            continue

        # first line that is plain text followed by structure -> "Title - Artist"
        if (
            detect_chords and not state["started"] and not song.title
            and len(s) <= 80 and not is_chord_line(ln) and _parse_inline(ln) is None
            and looks_structural(raw[i + 1] if i + 1 < n else "")
        ):
            s2 = re.sub(r"\s+chords?\s*$", "", s, flags=re.I)
            parts = re.split(r"\s+[-–—]\s+|\s+by\s+", s2, maxsplit=1, flags=re.I)
            song.title = parts[0].strip()
            if len(parts) > 1:
                song.artist = parts[1].strip()
            state["started"] = True
            i += 1
            continue

        if detect_chords and is_chord_line(ln):
            chords = chord_positions(ln)
            nxt = raw[i + 1].rstrip() if i + 1 < n else ""
            if nxt.strip() and not looks_structural(nxt):
                indent = len(nxt) - len(nxt.lstrip())
                chords = [(max(0, c - indent), ch) for c, ch in chords]
                add(Line(nxt.strip(), chords, "lyric"))
                i += 2
            else:
                add(Line("", chords, "chords"))
                i += 1
            continue

        if detect_chords:
            parsed = _parse_inline(ln)
            if parsed:
                lyr, ch = parsed
                add(Line(lyr, ch, "lyric" if lyr else "chords"))
                i += 1
                continue

        add(Line(lyrics=s))
        i += 1

    for sec in song.sections:
        while sec.lines and sec.lines[-1].kind == "blank":
            sec.lines.pop()
        while sec.lines and sec.lines[0].kind == "blank":
            sec.lines.pop(0)
    song.sections = [s for s in song.sections if s.name or s.lines]
    return song


# --------------------------------------------------------------------------- #
# File reading
# --------------------------------------------------------------------------- #


def _layout_from_boxes(boxes):
    """Rebuild a monospace-style text layout from OCR boxes.

    boxes: list of (x0, y0, x1, y1, text). Lines are grouped by vertical position and each
    box is placed at a column proportional to its x position, so chords stay over their words.
    """
    from statistics import median

    boxes = [b for b in boxes if b[4].strip()]
    if not boxes:
        return ""
    wide = [(b[2] - b[0]) / len(b[4]) for b in boxes if len(b[4]) >= 6] or [(b[2] - b[0]) / len(b[4]) for b in boxes]
    cw = max(median(wide), 1e-6)
    h = median(b[3] - b[1] for b in boxes)
    minx = min(b[0] for b in boxes)
    lines = []  # each: [mean_yc, [boxes]]
    for b in sorted(boxes, key=lambda b: (b[1] + b[3]) / 2):
        yc = (b[1] + b[3]) / 2
        if lines and abs(yc - lines[-1][0]) <= 0.6 * h:
            lines[-1][1].append(b)
            lines[-1][0] = sum((q[1] + q[3]) / 2 for q in lines[-1][1]) / len(lines[-1][1])
        else:
            lines.append([yc, [b]])
    out, prev_y = [], None
    for yc, bs in lines:
        if prev_y is not None and yc - prev_y > 2.2 * h:
            out.append("")  # visible gap -> blank line
        prev_y = yc
        s = ""
        for b in sorted(bs, key=lambda b: b[0]):
            col = int(round((b[0] - minx) / cw))
            s += " " * max(col - len(s), 1 if s else 0) + b[4].strip()
        out.append(s)
    return "\n".join(out)


_OCR_NEEDED = (
    "This PDF has no selectable text (it looks like a scan or photo). To read scanned PDFs, "
    "install the OCR add-on with:  pip install -r requirements-ocr.txt  and restart the app. "
    "Or export the chart as a text-based PDF or .txt file."
)


def _box(pts, text):
    xs, ys = [float(p[0]) for p in pts], [float(p[1]) for p in pts]
    return (min(xs), min(ys), max(xs), max(ys), str(text))


def _get_ocr():
    """Return fn(numpy_image) -> [(x0,y0,x1,y1,text)], using whichever RapidOCR package is installed."""
    try:  # new package (works on Python 3.13+):  pip install rapidocr onnxruntime
        from rapidocr import RapidOCR

        engine = RapidOCR()

        def run(arr):
            res = engine(arr)
            boxes, txts = getattr(res, "boxes", None), getattr(res, "txts", None)
            if boxes is None or txts is None:
                return []
            return [_box(p, t) for p, t in zip(boxes, txts)]

        return run
    except ImportError:
        pass
    try:  # old package (Python <= 3.12):  pip install rapidocr-onnxruntime
        from rapidocr_onnxruntime import RapidOCR as OldRapidOCR
    except ImportError:
        raise ValueError(_OCR_NEEDED) from None
    engine = OldRapidOCR()

    def run_old(arr):
        result, _ = engine(arr)
        return [_box(p, t) for p, t, _s in (result or [])]

    return run_old


def _ocr_pdf(pdf) -> str:
    import numpy as np

    run = _get_ocr()
    pages = []
    for page in pdf.pages:
        img = page.to_image(resolution=200).original.convert("RGB")
        pages.append(_layout_from_boxes(run(np.array(img))))
    text = "\n".join(pages)
    if not text.strip():
        raise ValueError("OCR could not find any text in this PDF.")
    return text


def _read_pdf(data: bytes) -> str:
    import pdfplumber

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        pages = []
        for page in pdf.pages:
            t = ""
            for kwargs in ({"layout": True}, {}):
                try:
                    t = page.extract_text(**kwargs) or ""
                except Exception:  # noqa: BLE001 - odd fonts/pages; try the next strategy
                    t = ""
                if t.strip():
                    break
            pages.append(t)
        text = "\n".join(pages)
        if not text.strip():
            text = _ocr_pdf(pdf)
    return "\n".join(l.rstrip() for l in text.split("\n"))


def read_upload(name: str, data: bytes) -> str:
    ext = name.lower().rsplit(".", 1)[-1] if "." in name else ""
    if ext == "docx":
        from docx import Document

        doc = Document(io.BytesIO(data))
        return "\n".join(p.text for p in doc.paragraphs)
    if ext == "pdf":
        return _read_pdf(data)
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("Could not decode file.")


# --------------------------------------------------------------------------- #
# Merging pasted lyrics into a chart
# --------------------------------------------------------------------------- #


def _norm(s: str) -> str:
    s = s.lower().replace("’", "'").replace("'", "")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s)).strip()


def _remap_chords(old: str, chords: list, new: str) -> list:
    """Keep each chord over the same word (and offset in word) when text changes."""
    ow = [m.span() for m in re.finditer(r"\S+", old)]
    nw = [m.span() for m in re.finditer(r"\S+", new)]
    if not ow or not nw:
        return [(0, ch) for _, ch in chords]
    out = []
    for col, ch in chords:
        wi = next((k for k, (a, b) in enumerate(ow) if a <= col < b), None)
        if wi is None:
            wi = next((k for k, (a, _) in enumerate(ow) if a >= col), len(ow) - 1)
        off = max(0, col - ow[wi][0])
        nwi = wi if len(ow) == len(nw) else min(len(nw) - 1, round(wi * len(nw) / len(ow)))
        a, b = nw[nwi]
        out.append((a + min(off, b - a), ch))
    return out


def merge_lyrics(chart: Song, lyr: Song, insert_missing: bool = True):
    """Replace chart lyric text with the pasted lyrics where lines match.

    Returns (chart, matched, total, inserted).
    """
    cands = [l.lyrics for s in lyr.sections for l in s.lines if l.kind == "lyric" and l.lyrics.strip()]
    cn = [_norm(c) for c in cands]
    matched = total = 0
    last = 0
    anchors = {}  # pasted index -> (section, line) of the chart line it matched
    used = set()
    for sec in chart.sections:
        for l in sec.lines:
            if l.kind != "lyric" or not l.lyrics.strip():
                continue
            total += 1
            o = _norm(l.lyrics)
            best_r, best_j = 0.0, -1
            for j in list(range(last, len(cands))) + list(range(0, last)):
                r = SequenceMatcher(None, o, cn[j]).ratio()
                if r > best_r + 1e-9:
                    best_r, best_j = r, j
                if r > 0.98:
                    break
            if best_r >= 0.6:
                matched += 1
                l.chords = _remap_chords(l.lyrics, l.chords, cands[best_j])
                l.lyrics = cands[best_j]
                anchors.setdefault(best_j, (sec, l))
                used.add(best_j)
                last = best_j + 1

    inserted = 0
    if insert_missing and matched:
        pending = {}  # id(chart line) -> count already inserted after it
        prev = None
        for j in range(len(cands)):
            if j in used:
                prev = anchors[j]
                continue
            new = Line(lyrics=cands[j])
            if prev is None:
                sec = chart.sections[0]
                sec.lines.insert(0, new)
            else:
                sec, anchor = prev
                k = pending.get(id(anchor), 0)
                idx = next(a for a, x in enumerate(sec.lines) if x is anchor)
                sec.lines.insert(idx + 1 + k, new)
                pending[id(anchor)] = k + 1
            inserted += 1
    return chart, matched, total, inserted


def build_song(chart_text, lyrics_text, transpose=0, flats=False, overrides=None, insert_missing=True):
    """Returns (song, notes:list[str])."""
    notes = []
    if chart_text and chart_text.strip():
        song = parse_chart(chart_text)
        if not song.has_chords():
            notes.append("No chords were detected in the uploaded chart.")
        if lyrics_text and lyrics_text.strip():
            lyr = parse_chart(lyrics_text, detect_chords=False)
            song, matched, total, inserted = merge_lyrics(song, lyr, insert_missing)
            if total == 0:
                notes.append("The chart has no lyric lines to match against the pasted lyrics.")
            else:
                notes.append(f"Matched {matched} of {total} chart lyric lines to the pasted lyrics.")
                if inserted:
                    notes.append(f"Inserted {inserted} pasted lyric line(s) that were missing from the chart (no chords).")
    else:
        song = parse_chart(lyrics_text, detect_chords=False)
        notes.append("Lyrics only, so there are no chords. Upload a chart to add them.")
    song = transpose_song(song, transpose, flats)
    for k, v in (overrides or {}).items():
        if v and v.strip():
            setattr(song, k, v.strip())
    return song, notes


# --------------------------------------------------------------------------- #
# Standard layout
# --------------------------------------------------------------------------- #


def chord_row(line: Line) -> str:
    if line.kind == "chords":
        return "   ".join(ch for _, ch in line.chords)
    out = ""
    for col, ch in sorted(line.chords, key=lambda x: x[0]):
        pad = col - len(out)
        if out:
            pad = max(pad, 1)
        out += " " * max(pad, 0) + ch
    return out


def rows_of(section: Section):
    """-> list of (kind, chord_text, lyric_text); kind in lyric|plain|chords|tab|blank"""
    rows = []
    for l in section.lines:
        if l.kind == "blank":
            rows.append(("blank", "", ""))
        elif l.kind == "tab":
            rows.append(("tab", "", l.lyrics))
        elif l.kind == "chords":
            rows.append(("chords", chord_row(l), ""))
        elif l.chords:
            rows.append(("lyric", chord_row(l), l.lyrics))
        else:
            rows.append(("plain", "", l.lyrics))
    return rows


def meta_line(song: Song) -> str:
    bits = []
    if song.key:
        bits.append(f"Key: {song.key}")
    if song.capo:
        bits.append(f"Capo: {song.capo}")
    for k in ("tempo", "bpm", "time", "tuning"):
        if song.extra.get(k):
            bits.append(f"{k.title() if k != 'bpm' else 'BPM'}: {song.extra[k]}")
    return "     ".join(bits)


def to_text(song: Song) -> str:
    out = []
    if song.title:
        out.append(f"Title: {song.title}")
    if song.artist:
        out.append(f"Artist: {song.artist}")
    if song.key:
        out.append(f"Key: {song.key}")
    if song.capo:
        out.append(f"Capo: {song.capo}")
    for k, v in song.extra.items():
        out.append(f"{k.title()}: {v}")
    for sec in song.sections:
        out.append("")
        if sec.name:
            out.append(f"[{sec.name}]")
        rows = rows_of(sec)
        for idx, (kind, ch, ly) in enumerate(rows):
            if kind == "lyric":
                out += [ch, ly]
            elif kind == "chords":
                out.append(ch)
                if idx + 1 < len(rows) and rows[idx + 1][0] == "plain":
                    out.append("")  # keep the plain lyric from pairing with these chords
            elif kind == "plain":
                out.append(ly)
            elif kind == "tab":
                out.append(ly)
            else:
                out.append("")
    return "\n".join(out).strip("\n") + "\n"


def to_html(song: Song, chord_color="#1d4ed8") -> str:
    e = html.escape
    parts = ['<div style="font-family:ui-monospace,Menlo,Consolas,monospace;font-size:14px;line-height:1.35;'
             'overflow-x:auto;padding:12px 16px;border:1px solid #8884;border-radius:8px;">']
    parts.append(f'<div style="font-family:sans-serif;font-size:24px;font-weight:700">{e(song.title or "Untitled")}</div>')
    if song.artist:
        parts.append(f'<div style="font-family:sans-serif;opacity:.7">{e(song.artist)}</div>')
    ml = meta_line(song)
    if ml:
        parts.append(f'<div style="font-family:sans-serif;font-size:13px;margin-top:4px">{e(ml)}</div>')
    parts.append("<pre style='margin:10px 0 0 0;font:inherit;white-space:pre'>")
    for sec in song.sections:
        if sec.name:
            parts.append(f'<div style="font-family:sans-serif;font-weight:700;margin:14px 0 4px;'
                         f'padding:2px 8px;background:#8882;border-radius:4px;color:{chord_color}">'
                         f'{e(sec.name.upper())}</div>')
        for kind, ch, ly in rows_of(sec):
            if kind == "blank":
                parts.append(" ")
            elif kind == "chords":
                parts.append(f'<b style="color:{chord_color}">{e(ch)}</b>')
            elif kind == "lyric":
                parts.append(f'<b style="color:{chord_color}">{e(ch)}</b>\n{e(ly)}')
            else:
                parts.append(e(ly))
    parts.append("</pre></div>")
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #


def _pdf_clean(s: str) -> str:
    s = s.replace("♯", "#").replace("♭", "b")
    return s.encode("cp1252", "replace").decode("cp1252")


def _fit_font(song: Song, font_size: float, usable_pt: float) -> float:
    maxc = 0
    for sec in song.sections:
        for _, ch, ly in rows_of(sec):
            maxc = max(maxc, len(ch), len(ly))
    if maxc == 0:
        return font_size
    return max(6.5, min(font_size, usable_pt / (0.6 * maxc)))


# row heights in units of font size: (chord row, lyric row)
_PDF_UNITS = {"lyric": (1.2, 1.25), "plain": (0, 1.35), "chords": (1.35, 0), "tab": (0, 1.15), "blank": (0.7, 0)}
_LINK_BLOCK_PT = 34  # vertical space reserved for the YouTube link line


def normalize_url(url: str) -> str:
    u = (url or "").strip()
    if u and not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    return u


def is_youtube_url(url: str) -> bool:
    from urllib.parse import urlparse

    host = (urlparse(normalize_url(url)).hostname or "").lower()
    return host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com")


def youtube_link_for(song: Song, url: str = ""):
    """-> (link_url, is_search). Uses the given video URL, else a YouTube search for the song."""
    from urllib.parse import quote_plus

    u = normalize_url(url)
    if u:
        return u, False
    q = " ".join(x for x in (song.title, song.artist) if x).strip()
    if not q:
        return "", False
    return "https://www.youtube.com/results?search_query=" + quote_plus(q), True


def _draw_song(c, song: Song, font_size: float, one_page: bool, youtube_url: str):
    from reportlab.lib.colors import Color
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.pdfbase.pdfmetrics import stringWidth

    W, H = letter
    M = 0.75 * inch
    top, bottom = H - 0.7 * inch, 0.8 * inch
    link_url, is_search = youtube_link_for(song, youtube_url)
    fs = _fit_font(song, font_size, W - 2 * M)
    if one_page:
        fixed = 30 + (18 if song.artist else 0) + (16 if meta_line(song) else 0) + 8
        fixed += _LINK_BLOCK_PT if link_url else 0
        units = sum(
            (2.3 if sec.name else 0) + sum(sum(_PDF_UNITS[k]) for k, _, _ in rows_of(sec))
            for sec in song.sections
        )
        if units:
            fs = max(6.0, min(fs, 0.97 * (top - bottom - fixed) / units))
    blue = Color(0.11, 0.25, 0.66)
    st = {"y": top, "page": 1}

    def footer():
        c.setFont("Helvetica", 8)
        c.setFillColor(Color(0.45, 0.45, 0.45))
        c.drawString(M, 0.45 * inch, _pdf_clean(song.title or ""))
        c.drawRightString(W - M, 0.45 * inch, f"Page {st['page']}")

    def newpage():
        footer()
        c.showPage()
        st["page"] += 1
        st["y"] = top

    def need(h):
        if st["y"] - h < bottom:
            newpage()

    heights = {k: (a * fs, b * fs) for k, (a, b) in _PDF_UNITS.items()}

    # Title block
    c.setFillColor(Color(0.05, 0.05, 0.05))
    c.setFont("Helvetica-Bold", 22)
    c.drawString(M, st["y"] - 22, _pdf_clean(song.title or "Untitled"))
    st["y"] -= 30
    if song.artist:
        c.setFont("Helvetica", 12)
        c.setFillColor(Color(0.35, 0.35, 0.35))
        c.drawString(M, st["y"] - 12, _pdf_clean(song.artist))
        st["y"] -= 18
    ml = meta_line(song)
    if ml:
        c.setFont("Helvetica-Bold", 10)
        c.setFillColor(Color(0.2, 0.2, 0.2))
        c.drawString(M, st["y"] - 10, _pdf_clean(ml))
        st["y"] -= 16
    c.setStrokeColor(Color(0.75, 0.75, 0.75))
    c.line(M, st["y"] - 2, W - M, st["y"] - 2)
    st["y"] -= 8

    for sec in song.sections:
        rows = rows_of(sec)
        sec_h = (2.4 * fs if sec.name else 0) + sum(sum(heights[k]) for k, _, _ in rows)
        if sec_h <= top - bottom and st["y"] - sec_h < bottom:
            newpage()
        if sec.name:
            need(3.0 * fs)
            st["y"] -= 0.5 * fs
            c.setFillColor(Color(0.91, 0.93, 0.98))
            c.rect(M, st["y"] - 1.5 * fs, W - 2 * M, 1.5 * fs, fill=1, stroke=0)
            c.setFillColor(blue)
            c.setFont("Helvetica-Bold", fs + 0.5)
            c.drawString(M + 4, st["y"] - 1.08 * fs, _pdf_clean(sec.name.upper()))
            st["y"] -= 1.8 * fs
        for kind, ch, ly in rows:
            h1, h2 = heights[kind]
            need(h1 + h2)
            y = st["y"]
            if kind == "blank":
                pass
            elif kind in ("lyric", "chords"):
                c.setFont("Courier-Bold", fs)
                c.setFillColor(blue)
                c.drawString(M, y - fs, _pdf_clean(ch))
            if kind in ("lyric", "plain", "tab"):
                c.setFont("Courier", fs)
                c.setFillColor(Color(0, 0, 0))
                c.drawString(M, y - h1 - fs, _pdf_clean(ly))
            st["y"] -= h1 + h2

    # Clickable YouTube link after the end of the chart
    if link_url:
        need(_LINK_BLOCK_PT)
        lfs = 11
        what = " - ".join(x for x in (song.title, song.artist) if x)
        prefix = "Search YouTube: " if is_search else "Watch on YouTube: "
        label = _pdf_clean(prefix + what)
        while stringWidth(label, "Helvetica", lfs) > W - 2 * M and len(label) > len(prefix) + 4:
            label = label[: -4] + "..."
        y = st["y"] - 14
        c.setFont("Helvetica", lfs)
        c.setFillColor(blue)
        c.drawString(M, y - lfs, label)
        tw = stringWidth(label, "Helvetica", lfs)
        c.setStrokeColor(blue)
        c.setLineWidth(0.6)
        c.line(M, y - lfs - 1.5, M + tw, y - lfs - 1.5)
        c.linkURL(link_url, (M, y - lfs - 3, M + tw, y + 2), relative=0, thickness=0)
        st["y"] = y - lfs - 6
    footer()
    c.showPage()


def render_pdf_many(items, font_size: float = 14, one_page: bool = False) -> bytes:
    """items: [(Song, youtube_url)]. Each song starts on a new page, with its own page numbers."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    if len(items) == 1:
        c.setTitle(items[0][0].title or "Chart")
        c.setAuthor(items[0][0].artist or "")
    else:
        c.setTitle("Song charts")
    for song, url in items:
        _draw_song(c, song, font_size, one_page, url)
    c.save()
    return buf.getvalue()


def render_pdf(song: Song, font_size: float = 14, one_page: bool = False, youtube_url: str = "") -> bytes:
    """one_page=True shrinks the font (down to 6pt) so the whole song fits on a single page.

    A clickable YouTube link is added after the chart: youtube_url if given, otherwise a
    YouTube search for the song's title and artist."""
    return render_pdf_many([(song, youtube_url)], font_size, one_page)


# --------------------------------------------------------------------------- #
# DOCX
# --------------------------------------------------------------------------- #

_PPR_AFTER_SHD = (
    "w:tabs", "w:suppressAutoHyphens", "w:kinsoku", "w:wordWrap", "w:overflowPunct", "w:topLinePunct",
    "w:autoSpaceDE", "w:autoSpaceDN", "w:bidi", "w:adjustRightInd", "w:snapToGrid", "w:spacing", "w:ind",
    "w:contextualSpacing", "w:mirrorIndents", "w:suppressOverlap", "w:jc", "w:textDirection",
    "w:textAlignment", "w:textboxTightWrap", "w:outlineLvl", "w:divId", "w:cnfStyle", "w:rPr",
    "w:sectPr", "w:pPrChange",
)


def render_docx(song: Song, font_size: float = 14, one_page: bool = False) -> bytes:
    """one_page=True estimates the font size (down to 6pt) so the song fits on one page.

    Word does its own layout, so the fit is an estimate with a safety margin."""
    from docx import Document
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Inches, Pt, RGBColor

    doc = Document()
    sect = doc.sections[0]
    sect.page_width, sect.page_height = Inches(8.5), Inches(11)
    for side in ("left_margin", "right_margin"):
        setattr(sect, side, Inches(0.75))
    sect.top_margin = sect.bottom_margin = Inches(0.7)
    doc.core_properties.title = song.title or "Chart"
    doc.core_properties.author = song.artist or ""
    fs = _fit_font(song, font_size, 7.0 * 72)
    if one_page:
        # (a, b): height = a * font_size + b points
        coef = {"lyric": (2.27, 2), "plain": (1.14, 1), "chords": (1.14, 2), "tab": (1.14, 0), "blank": (0.7, 0)}
        A = 0.0
        B = 29 + (17 if song.artist else 0) + (16 if meta_line(song) else 0)
        for sec in song.sections:
            if sec.name:
                A, B = A + 1.22, B + 13
            for k, _, _ in rows_of(sec):
                A, B = A + coef[k][0], B + coef[k][1]
        if A:
            fs = max(6.0, min(fs, 0.88 * (9.6 * 72 - B) / A))
    doc.styles["Normal"].font.size = Pt(6)  # keeps empty/spacer paragraphs from adding 11pt of height
    blue = RGBColor(0x1C, 0x40, 0xA8)

    def para(before=0, after=0, keep=False):
        p = doc.add_paragraph()
        pf = p.paragraph_format
        pf.space_before, pf.space_after = Pt(before), Pt(after)
        pf.line_spacing = 1.0
        pf.keep_with_next = keep
        pf.keep_together = True
        return p

    def run(p, text, font="Courier New", size=None, bold=False, italic=False, color=None):
        r = p.add_run(text)
        r.font.name = font
        r._element.rPr.rFonts.set(qn("w:eastAsia"), font)
        r.font.size = Pt(size or fs)
        r.bold, r.italic = bold, italic
        if color is not None:
            r.font.color.rgb = color
        return r

    p = para(after=2)
    run(p, song.title or "Untitled", "Calibri", 22, bold=True)
    if song.artist:
        p = para(after=2)
        run(p, song.artist, "Calibri", 12, color=RGBColor(0x59, 0x59, 0x59))
    ml = meta_line(song)
    if ml:
        p = para(after=4)
        run(p, ml, "Calibri", 10, bold=True, color=RGBColor(0x33, 0x33, 0x33))

    for sec in song.sections:
        if sec.name:
            p = para(before=10, after=3, keep=True)
            run(p, sec.name.upper(), "Calibri", fs + 0.5, bold=True, color=blue)
            pPr = p._p.get_or_add_pPr()
            shd = OxmlElement("w:shd")
            shd.set(qn("w:val"), "clear")
            shd.set(qn("w:color"), "auto")
            shd.set(qn("w:fill"), "E8EDF9")
            pPr.insert_element_before(shd, *_PPR_AFTER_SHD)
        rows = rows_of(sec)
        for idx, (kind, ch, ly) in enumerate(rows):
            nxt_is_row = idx + 1 < len(rows) and rows[idx + 1][0] != "blank"
            if kind == "blank":
                p = para()
                run(p, "", size=fs * 0.6)
            elif kind == "chords":
                p = para(before=2, keep=nxt_is_row)
                run(p, ch, bold=True, color=blue)
            elif kind == "lyric":
                p = para(before=2, keep=True)
                run(p, ch, bold=True, color=blue)
                p = para(keep=nxt_is_row)
                run(p, ly)
            else:
                p = para(before=1 if kind == "plain" else 0, keep=nxt_is_row)
                run(p, ly)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
