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
    return shutil.which("pdftotext") is not None


# ---------------- PDF ----------------

def extract_pdf(path: Path, ocr: bool = False, ocr_lang: str = "eng") -> Extracted:
    ex = Extracted()
    meta = _pdfinfo(path)
    ex.title = meta.get("Title", "").strip()
    cd = meta.get("CreationDate", "")
    m = re.search(r"D(\d{4})", cd)
    if m:
        ex.year = m.group(1)
        ex.doc_date = cd[:10]
    if not has_poppler():
        ex.note = "pdftotext not installed (apt install poppler-utils)"
        return ex
    raw = subprocess.run(
        ["pdftotext", "-enc", "UTF-8", str(path), "-"],
        capture_output=True, text=True, errors="replace", timeout=300,
    )
    text = raw.stdout or ""
    pages = [normalize_text(p) for p in text.split("\x0c")]
    pages = [p for p in pages if p]
    total_chars = sum(len(p) for p in pages)
    npages_meta = int(meta.get("Pages", "0") or 0)
    if total_chars < 200 and npages_meta > 0:
        # likely scanned
        if ocr and shutil.which("ocrmypdf"):
            return _ocr_pdf(path, meta)
        ex.note = f"no text layer ({npages_meta} pages) — scanned? enable [index] ocr=true or OCR the file"
        return ex
    for i, p in enumerate(pages, 1):
        ex.units.append((f"p.{i}", p))
    return ex.finalize()


def _ocr_pdf(path: Path, meta: dict) -> Extracted:
    import tempfile
    ex = Extracted()
    ex.note = "OCR'd at index time"
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "ocr.pdf"
        subprocess.run(
            ["ocrmypdf", "--skip-text", "--quiet", str(path), str(out)],
            capture_output=True, text=True, timeout=1800,
        )
        text = subprocess.run(
            ["pdftotext", "-enc", "UTF-8", str(out), "-"],
            capture_output=True, text=True, errors="replace", timeout=300,
        ).stdout
    for i, p in enumerate([normalize_text(p) for p in text.split("\x0c") if normalize_text(p)], 1):
        ex.units.append((f"p.{i}", p))
    return ex.finalize()


def _pdfinfo(path: Path) -> dict:
    if not shutil.which("pdfinfo"):
        return {}
    try:
        out = subprocess.run(["pdfinfo", str(path)], capture_output=True, text=True, timeout=30).stdout
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
