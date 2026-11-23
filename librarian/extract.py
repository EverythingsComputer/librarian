"""Format extractors. Each returns Extracted: units=[(label, text)], plus metadata.

Labels are page/slide markers so retrieved snippets can cite locations.
"""
from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Extracted:
    units: list[tuple[str, str]] = field(default_factory=list)   # (label, text)
    title: str = ""
    year: str = ""
    doc_date: str = ""
    note: str = ""
    full_text: str = ""
    pages: int = 0

    def finalize(self):
        self.full_text = "\n".join(t for _, t in self.units)
        self.pages = len(self.units)
        return self


def normalize_text(s: str) -> str:
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def has_poppler() -> bool:
    return tool_path("pdftotext") is not None


def _bundle_bin():
    """The app's own bin dir when it runs as a bundle (AppImage/PyInstaller)."""
    if getattr(sys, "frozen", False) or os.environ.get("APPIMAGE"):
        return Path(sys.executable).resolve().parent
    return None


def fix_mojibake(s: str) -> str:
    """Repair UTF-8 bytes misread as cp1252/latin-1 (classic PDF /Title bug):
    'Ù—' back to 'اٗ'. Only accepted when the round-trip yields real text and
    changes the string; correctly-decoded titles pass through untouched (B2)."""
    if not s or all(ord(c) < 128 for c in s):
        return s
    for enc in ("cp1252", "latin-1"):
        try:
            b = s.encode(enc)
        except UnicodeEncodeError:
            continue
        try:
            r = b.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if r != s:
            return r
    return s


def tool_path(name: str):
    """Resolve an external helper (pdftotext, pdfinfo, ocrmypdf). Inside a
    bundle the launcher puts the bundled bin dir first on PATH (v0.2.5), so
    bundled poppler wins over a host one the bundled loader env would kill.
    v0.2.9: when the bundle is started by its direct path (the UI 'Start
    command' prints /tmp/.mount_*/usr/bin/librarian ... — no AppRun, so PATH
    is untouched), also look next to the executable itself."""
    found = shutil.which(name)
    if found:
        return found
    bd = _bundle_bin()
    if bd is not None:
        cand = bd / name
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def tool_env(path=None) -> dict:
    """Child environment for external tools. A host binary must NEVER inherit
    the bundle's LD_LIBRARY_PATH/LD_PRELOAD: the bundled libstdc++ and poppler
    deps fail against the host glibc, the tool dies at load, and stdout comes
    back empty — that is how every PDF on the Optiplex indexed itself as a
    silent empty stub (v0.2.5). Tools from our own bundle keep the env."""
    env = os.environ.copy()
    bd = _bundle_bin()
    if bd is None or path is None:
        return env
    try:
        Path(path).resolve().relative_to(bd)
        return env
    except ValueError:
        pass
    env.pop("LD_LIBRARY_PATH", None)
    env.pop("LD_PRELOAD", None)
    return env


# ---------------- PDF ----------------

def extract_pdf(path: Path, ocr: bool = False, ocr_lang: str = "eng") -> Extracted:
    ex = Extracted()
    meta = _pdfinfo(path)
    ex.title = fix_mojibake(meta.get("Title", "").strip())
    cd = meta.get("CreationDate", "")
    m = re.search(r"D(\d{4})", cd)
    if m:
        ex.year = m.group(1)
        ex.doc_date = cd[:10]
    if not has_poppler():
        ex.note = ("pdftotext not found — install poppler-utils "
                   "(the AppImage bundles its own and uses it automatically)")
        return ex
    pdf_bin = tool_path("pdftotext")
    raw = subprocess.run(
        [pdf_bin, "-enc", "UTF-8", str(path), "-"],
        capture_output=True, text=True, errors="replace", timeout=300,
        env=tool_env(pdf_bin),
    )
    text = raw.stdout or ""
    if raw.returncode != 0 and not text.strip():
        # never fail silently: say exactly what the tool said (v0.2.5)
        ex.note = (f"pdftotext failed (exit {raw.returncode}): "
                   f"{(raw.stderr or '').strip()[:200]}")
        return ex
    pages = [normalize_text(p) for p in text.split("\x0c")]
    pages = [p for p in pages if p]
    total_chars = sum(len(p) for p in pages)
    npages_meta = int(meta.get("Pages", "0") or 0)
    if total_chars == 0 and npages_meta > 0:
        # genuinely no characters came out: scanned (or an image-only PDF)
        if ocr:
            if ocr_available():
                return _ocr_pdf(path, meta, ocr_lang)
            ex.note = (f"no text layer ({npages_meta} pages) — scanned; OCR is ON "
                       f"but tesseract/pdftoppm is unavailable here")
            return ex
        ex.note = (f"no text layer ({npages_meta} pages) — scanned? turn on OCR "
                   f"in the web page or run: sync --ocr")
        return ex
    if total_chars < 200 and npages_meta > 3:
        # sparse but REAL text: index it, warn softly (v0.2.6: the old
        # <200-equals-scanned rule threw away extracted text entirely)
        ex.note = (f"very little text for {npages_meta} pages "
                   f"({total_chars} chars) — mostly images?")
    for i, p in enumerate(pages, 1):
        ex.units.append((f"p.{i}", p))
    return ex.finalize()


def tessdata_dir():
    """Where eng.traineddata lives: explicit env, then the bundle
    (usr/share/tessdata next to our bin dir), then common host locations."""
    cands = []
    env = os.environ.get("TESSDATA_PREFIX")
    if env:
        cands.append(Path(env))
    bd = _bundle_bin()
    if bd is not None:
        cands.append(bd.parent / "share" / "tessdata")
    cands += [Path("/usr/share/tesseract-ocr/5/tessdata"),
              Path("/usr/share/tesseract-ocr/6/tessdata"),
              Path("/usr/share/tessdata")]
    for c in cands:
        if (c / "eng.traineddata").exists():
            return str(c)
    return None


def ocr_available() -> bool:
    return bool(tool_path("tesseract") and tool_path("pdftoppm"))


def _ocr_pdf(path: Path, meta: dict, lang: str = "eng") -> Extracted:
    """OCR for image-only PDFs: pdftoppm renders pages, tesseract reads them.
    v0.2.7 replaces the ocrmypdf route: no Ghostscript to bundle, and the
    user's file is never rewritten — OCR text goes into the index only."""
    import tempfile
    ex = Extracted()
    ppm, tess = tool_path("pdftoppm"), tool_path("tesseract")
    npages = int(meta.get("Pages", "0") or 0)
    max_pages = min(npages or 500, 500)
    with tempfile.TemporaryDirectory() as td:
        r = subprocess.run(
            [ppm, "-r", "200", "-png", "-l", str(max_pages), str(path),
             str(Path(td) / "pg")],
            capture_output=True, text=True, timeout=1800, env=tool_env(ppm))
        if r.returncode != 0:
            ex.note = (f"OCR render failed (exit {r.returncode}): "
                       f"{(r.stderr or '').strip()[:160]}")
            return ex
        pngs = sorted(Path(td).glob("pg-*.png"))
        td_dir = tessdata_dir()
        for i, png in enumerate(pngs, 1):
            cmd = [tess, str(png), "-", "-l", lang]
            if td_dir:
                cmd += ["--tessdata-dir", td_dir]
            try:
                out = subprocess.run(cmd, capture_output=True, text=True,
                                     errors="replace", timeout=120,
                                     env=tool_env(tess)).stdout
            except Exception:
                out = ""
            t = normalize_text(out)
            if t:
                ex.units.append((f"p.{i}", t))
    if ex.units:
        ex.note = "OCR'd at index time (search text only; your file is unchanged)"
        return ex.finalize()
    ex.note = f"no text layer ({npages or len(pngs)} pages) — OCR found no readable text"
    return ex


def _pdfinfo(path: Path) -> dict:
    info_bin = tool_path("pdfinfo")
    if not info_bin:
        return {}
    try:
        out = subprocess.run([info_bin, str(path)], capture_output=True,
                             text=True, timeout=30, env=tool_env(info_bin)).stdout
    except Exception:
        return {}
    info = {}
    for line in out.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            info[k.strip()] = v.strip()
    return info


# ---------------- PPTX ----------------

def extract_pptx(path: Path) -> Extracted:
    from pptx import Presentation
    ex = Extracted()
    try:
        prs = Presentation(str(path))
    except Exception as e:
        ex.note = f"pptx open failed: {e}"
        return ex
    cp = prs.core_properties
    ex.title = (cp.title or "").strip()
    if cp.created:
        ex.year = str(cp.created.year)
        ex.doc_date = cp.created.isoformat()[:10]
    for i, slide in enumerate(prs.slides, 1):
        parts = []
        for sh in slide.shapes:
            if sh.has_text_frame:
                t = sh.text_frame.text.strip()
                if t:
                    parts.append(t)
            if sh.shape_type == 19 and getattr(sh, "has_table", False):  # table
                for row in sh.table.rows:
                    parts.append(" | ".join(c.text.strip() for c in row.cells))
        try:
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
                nt = slide.notes_slide.notes_text_frame.text.strip()
                if nt:
                    parts.append("[notes] " + nt)
        except Exception:
            pass
        txt = normalize_text("\n".join(parts))
        if txt:
            ex.units.append((f"slide {i}", txt))
    return ex.finalize()


# ---------------- DOCX ----------------

def extract_docx(path: Path) -> Extracted:
    import docx
    ex = Extracted()
    try:
        d = docx.Document(str(path))
    except Exception as e:
        ex.note = f"docx open failed: {e}"
        return ex
    cp = d.core_properties
    ex.title = (cp.title or "").strip()
    if cp.created:
        ex.year = str(cp.created.year)
        ex.doc_date = cp.created.isoformat()[:10]
    parts = []
    for para in d.paragraphs:
        if para.text.strip():
            style = (para.style.name or "").lower() if para.style is not None else ""
            prefix = "# " if "heading" in style else ""
            parts.append(prefix + para.text.strip())
    for tbl in d.tables:
        for row in tbl.rows:
            parts.append(" | ".join(c.text.strip() for c in row.cells))
    # one unit per ~2000 chars, label sections
    text = normalize_text("\n".join(parts))
    if text:
        ex.units = _split_by_size(text, 2000, "para")
    return ex.finalize()


# ---------------- MSG (Outlook email) ----------------

def extract_msg(path: Path) -> Extracted:
    import extract_msg
    ex = Extracted()
    try:
        m = extract_msg.Message(str(path))
    except Exception as e:
        ex.note = f"msg open failed: {e}"
        return ex
    lines = []
    if m.subject:
        ex.title = m.subject.strip()
        lines.append(f"SUBJECT: {m.subject}")
    if m.date:
        lines.append(f"DATE: {m.date}")
        ex.doc_date = str(m.date)[:10]
        try:
            ex.year = str(m.date.year)
        except Exception:
            pass
    if m.sender:
        lines.append(f"FROM: {m.sender}")
    if m.to:
        lines.append(f"TO: {m.to}")
    body = normalize_text(m.body or "")
    if body:
        lines.append("")
        lines.append(body)
    atts = [a.name for a in (m.attachments or [])]
    if atts:
        lines.append(f"\nATTACHMENTS: {', '.join(atts)}")
    text = normalize_text("\n".join(lines))
    if text:
        ex.units = _split_by_size(text, 2000, "part")
    m.close()
    return ex.finalize()


# ---------------- plain text / md / csv / html ----------------

_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")

def extract_text(path: Path) -> Extracted:
    ex = Extracted()
    data = path.read_bytes()
    if path.suffix.lower() in (".html", ".htm"):
        s = data.decode("utf-8", errors="replace")
        s = _TAG_RE.sub(" ", s)
        s = _TAG.sub(" ", s)
        s = re.sub(r"&nbsp;?", " ", s)
    elif path.suffix.lower() in (".csv", ".tsv"):
        delim = "\t" if path.suffix.lower() == ".tsv" else ","
        try:
            rows = list(csv.reader(io.StringIO(data.decode("utf-8", errors="replace")), delimiter=delim))
            s = "\n".join(" | ".join(r) for r in rows)
        except Exception:
            s = data.decode("utf-8", errors="replace")
    else:
        s = data.decode("utf-8", errors="replace")
    text = normalize_text(s)
    if text:
        ex.units = _split_by_size(text, 2000, "part")
    return ex.finalize()


# ---------------- ZIP (opt-in) ----------------

def extract_zip(path: Path) -> Extracted:
    """Only text-y members, bounded count/size."""
    ex = Extracted(note="zip contents")
    try:
        zf = zipfile.ZipFile(path)
    except Exception as e:
        ex.note = f"zip open failed: {e}"
        return ex
    count = 0
    for zi in zf.infolist():
        if zi.is_dir() or zi.file_size > 5_000_000 or count >= 200:
            continue
        name = zi.filename
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in TEXT_EXTS_LOCAL:
            continue
        try:
            raw = zf.read(zi).decode("utf-8", errors="replace")
        except Exception:
            continue
        t = normalize_text(raw)
        if t:
            ex.units.append((name, t))
            count += 1
    return ex.finalize()


TEXT_EXTS_LOCAL = {"txt", "md", "csv", "tsv", "json", "html", "htm", "xml", "yaml", "yml", "py", "c", "h", "ini", "cfg", "sh"}


def _split_by_size(text: str, target: int, label: str) -> list[tuple[str, str]]:
    """Split big flat text into ~target-size units at paragraph/line boundaries."""
    if len(text) <= target:
        return [(label + " 1", text)]
    units, buf, n = [], "", 1
    for chunk in re.split(r"\n{2,}", text):
        if len(buf) + len(chunk) + 2 > target and buf:
            units.append((f"{label} {n}", buf.strip()))
            buf, n = "", n + 1
        while len(chunk) > target:  # oversized paragraph
            cut = chunk.rfind("\n", 0, target) or target
            if buf:
                units.append((f"{label} {n}", buf.strip()))
                buf, n = "", n + 1
            units.append((f"{label} {n}", chunk[:cut].strip()))
            n += 1
            chunk = chunk[cut:].strip()
        buf = (buf + "\n\n" + chunk).strip()
    if buf:
        units.append((f"{label} {n}", buf))
    return units
