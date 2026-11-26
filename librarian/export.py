"""Export searchable copies (v0.4.0).

For every SCANNED pdf in scope (indexed with zero text chars) produce
`<name>.searchable.pdf` — the original pages with a real, invisible OCR text
layer baked in, so the copy carries its own searchability to any device and
any reader. Original files are never modified; already-searchable files
(text-layer PDFs, docx/pptx/txt...) are skipped and reported, not copied.

Engine (no Ghostscript needed): pdftoppm -> tesseract word boxes (TSV) ->
invisible-text overlay via reportlab (text render mode 3) -> merged over each
source page with pypdf. Same tesseract engine that powers indexing, page
coordinates scaled from the 300-dpi render back to PDF points.

Progress mirrors sync: EXPORT_PROGRESS + progress_snapshot() for UI/MCP, and
per-file '> [i/N] name' lines on the CLI. A cancel event stops between files
and between pages.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

from .config import Config
from .db import connect
from .extract import tessdata_dir, tool_env, tool_path

EXPORT_LOCK = threading.Lock()
EXPORT_CANCEL = threading.Event()
EXPORT_PROGRESS: dict = {"active": False}

_DPI = 300


def export_progress_snapshot() -> dict:
    snap = dict(EXPORT_PROGRESS)
    if snap.get("active") and snap.get("file_started"):
        snap["file_seconds"] = round(time.time() - snap["file_started"], 1)
    snap.setdefault("active", False)
    snap.setdefault("done", 0)
    snap.setdefault("total", 0)
    snap.setdefault("file", "")
    return snap


def _ep(**kw) -> None:
    base = dict(EXPORT_PROGRESS)
    base.update(kw)
    EXPORT_PROGRESS.clear()
    EXPORT_PROGRESS.update(base)


def _file_text(path: str):
    """True/False: does the FILE itself carry extractable text?
    None if pdftotext can't answer (fall back to the index heuristic).
    The index cannot answer this once OCR is on: an OCR'd scan shows
    chars>0 exactly like a native text PDF, yet its file still has no
    layer — and that is precisely what an export copy must add."""
    bin = tool_path("pdftotext")
    if not bin:
        return None
    try:
        out = subprocess.run([bin, "-q", path, "-"], capture_output=True,
                             text=True, errors="replace", timeout=180,
                             env=tool_env(bin)).stdout
    except Exception:
        return None
    return bool(out.strip())


def exportable_docs(cfg: Config, doc_ids: list[int] | None = None,
                    roots: list[str] | None = None, report_progress=False):
    """(candidates, skipped) — candidates are PDFs whose FILE has no text
    layer (scanned, whether or not the index OCR'd it) with files present;
    skipped lists everything else that was asked for with a reason, so the
    admin sees exactly what 'export' did and did not do."""
    if not cfg.db_path.exists():
        return [], []
    db = connect(cfg.db_path, read_only=True)
    conds, args = ["d.dup_of IS NULL", "lower(d.ext)='pdf'"], []
    if doc_ids:
        conds.append("d.id IN (" + ",".join("?" * len(doc_ids)) + ")")
        args += list(doc_ids)
    if roots:
        conds.append("d.root IN (" + ",".join("?" * len(roots)) + ")")
        args += list(roots)
    rows = [dict(r) for r in db.execute(
        f"SELECT d.id, d.path, d.root, d.title, d.kind, d.chars FROM docs d "
        f"WHERE {' AND '.join(conds)} ORDER BY d.root, d.path", args).fetchall()]
    db.close()
    cand, skip = [], []
    n = len(rows)
    for i, r in enumerate(rows, 1):
        if report_progress:
            _ep(active=True, done=i - 1, total=n, file=Path(r["path"]).name,
                detail="checking")
        if not os.path.isfile(r["path"]):
            skip.append({**r, "why": "file missing on disk"})
            continue
        if (r["chars"] or 0) == 0:
            cand.append(r)                    # no text anywhere: bake candidate
            continue
        has = _file_text(r["path"])           # index says text; does the FILE?
        if has is False:
            cand.append(r)                    # OCR lives only in the index
        else:
            skip.append({**r, "why": "PDF already has a text layer"})
    return cand, skip


def _render_page(pdf: str, page_no: int, outdir: str) -> str:
    """one page -> 300-dpi PNG; returns its path."""
    stem = os.path.join(outdir, "pg-")
    for stale in os.listdir(outdir):
        if stale.startswith("pg") and stale.endswith(".png"):
            try:
                os.remove(os.path.join(outdir, stale))
            except OSError:
                pass
    subprocess.run([tool_path("pdftoppm"), "-r", str(_DPI), "-png",
                    "-f", str(page_no), "-l", str(page_no), pdf, stem],
                   check=True, capture_output=True, env=tool_env(tool_path("pdftoppm")))
    for f in sorted(os.listdir(outdir)):
        if f.startswith("pg-") and f.endswith(".png"):
            p = os.path.join(outdir, f)
            os.rename(p, os.path.join(outdir, "page.png"))
            return os.path.join(outdir, "page.png")
    raise RuntimeError(f"pdftoppm produced no image for page {page_no}")


def _tesseract_words(png: str) -> list[tuple[int, int, int, int, str]]:
    cmd = [tool_path("tesseract"), png, "-", "tsv"]
    td = tessdata_dir()
    if td:
        cmd += ["--tessdata-dir", td]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True,
                         env=tool_env(tool_path("tesseract"))).stdout
    words = []
    for line in out.splitlines()[1:]:          # skip TSV header
        parts = line.split("\t")
        if len(parts) < 12:
            continue
        # TSV columns: level page block par line word left top width height conf text
        level, text = parts[0], parts[11].strip()
        if level == "5" and text:              # word rows
            try:
                words.append((int(parts[6]), int(parts[7]),
                              int(parts[8]), int(parts[9]), text))
            except ValueError:
                continue
    return words


def _overlay_pdf(pages_words: list[list], page_sizes: list[tuple[int, int]],
                 out_path: str) -> bool:
    """invisible text layer PDF sized to match the source pages."""
    from reportlab.lib.colors import black
    from reportlab.pdfgen import canvas
    s = 72.0 / _DPI                      # render px -> PDF points
    c = canvas.Canvas(out_path)
    any_word = False
    for words, (pw_pt, ph_pt) in zip(pages_words, page_sizes):
        ph_px = ph_pt / s
        c.setPageSize((pw_pt, ph_pt))
        if hasattr(c, "setTextRenderMode"):
            c.setTextRenderMode(3)       # invisible
        else:                            # raw operator (must be str: reportlab
            c._code.append("3 Tr")       # joins _code with '\n', no bytes)
        c.setFillColor(black)
        for left, top, width, height, text in words:
            fs = max(4.0, height * s * 0.92)
            c.setFont("Helvetica", fs)
            y = ph_px - (top + height)   # flip: PDF origin bottom-left
            c.drawString(left * s, y * s, text)
            any_word = True
        c.showPage()
    c.save()
    return any_word


def export_one(src: str, dst: str, cancel: threading.Event | None = None) -> int:
    """OCR-bake one scanned PDF; returns word count. Raises RuntimeError."""
    import tempfile
    from pypdf import PdfReader, PdfWriter

    for t, pkg in (("pdftoppm", "poppler-utils"), ("tesseract", "tesseract-ocr")):
        if tool_path(t) is None:
            raise RuntimeError(
                f"export needs '{t}' — the AppImage bundles it; from source "
                f"install {pkg} (and set TESSDATA_PREFIX if needed)")
    reader = PdfReader(src)
    n = len(reader.pages)
    sizes = []
    for pg in reader.pages:
        box = pg.mediabox
        w = float(box.width) * float(pg.get("/UserUnit", 1) or 1)
        h = float(box.height) * float(pg.get("/UserUnit", 1) or 1)
        sizes.append((w, h))
    pages_words: list[list] = []
    words_total = 0
    with tempfile.TemporaryDirectory(prefix="librexport-") as td:
        for i in range(1, n + 1):
            if cancel is not None and cancel.is_set():
                raise RuntimeError("cancelled")
            png = _render_page(src, i, td)
            words = _tesseract_words(png)
            pages_words.append(words)
            words_total += len(words)
            try:
                os.remove(png)
            except OSError:
                pass
        if words_total == 0:
            raise RuntimeError("OCR found no readable text — nothing to bake in")
        ov = os.path.join(td, "overlay.pdf")
        if not _overlay_pdf(pages_words, sizes, ov):
            raise RuntimeError("overlay build produced nothing")
        overlay = PdfReader(ov)
        # pypdf 7: pages must live in the writer BEFORE merging
        writer = PdfWriter(clone_from=src)
        for i in range(min(len(writer.pages), len(overlay.pages))):
            writer.pages[i].merge_page(overlay.pages[i])
        with open(dst, "wb") as f:
            writer.write(f)
    return words_total


def run_export(cfg: Config, out_dir: str | Path | None = None,
               doc_ids: list[int] | None = None, roots: list[str] | None = None,
               whole_library: bool = False, print_lines: bool = False) -> dict:
    """Synchronous export (callers own EXPORT_LOCK / cancel). roots=whole
    folder(s); whole_library = every folder; doc_ids = explicit picks."""
    t0 = time.time()
    if not whole_library and not doc_ids and not roots:
        raise ValueError("nothing selected: pass roots, doc_ids or whole_library")
    if roots and not whole_library:
        known = {r.name for r in cfg.roots}
        bad = [r for r in roots if r not in known]
        if bad:
            raise ValueError(f"unknown folder(s): {', '.join(bad)}. "
                             f"Known: {', '.join(sorted(known)) or '(none)'}")
    cand, skipped = exportable_docs(
        cfg, doc_ids=doc_ids, roots=None if whole_library else roots,
        report_progress=True)
    out = Path(out_dir or (cfg.data_dir / "exports")).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    EXPORT_CANCEL.clear()
    done, failed, existing = [], [], []
    _ep(active=True, done=0, total=len(cand), file="", detail="export",
        started=time.time(), file_started=None)
    try:
        for i, d in enumerate(cand, 1):
            if EXPORT_CANCEL.is_set():
                break
            src = d["path"]
            dst = out / (Path(src).stem + ".searchable.pdf")
            if print_lines:
                mb = os.path.getsize(src) / 1e6 if os.path.isfile(src) else 0
                print(f"> [{i}/{len(cand)}] {Path(src).name} (export, {mb:.1f} MB)",
                      flush=True)
            _ep(done=i - 1, total=len(cand), file=Path(src).name,
                file_started=time.time())
            if dst.exists():
                existing.append(str(dst))
                continue
            t1 = time.time()
            try:
                w = export_one(src, str(dst), cancel=EXPORT_CANCEL)
                done.append({"doc_id": d["id"], "out": str(dst), "words": w})
            except RuntimeError as e:
                if str(e) == "cancelled":
                    EXPORT_CANCEL.set()
                    break
                failed.append({"doc_id": d["id"], "path": src, "error": str(e)[:300]})
            if print_lines and time.time() - t1 > 15:
                print(f"   ^ finished in {time.time() - t1:.0f}s", flush=True)
            if EXPORT_CANCEL.is_set() and not any(x["out"] == str(dst) for x in done):
                try:
                    dst.unlink(missing_ok=True)   # partial file from a cancelled job
                except OSError:
                    pass
    finally:
        _ep(active=False, done=len(done) + len(failed) + len(existing),
            total=len(cand), file="", file_started=None)
    cancelled = EXPORT_CANCEL.is_set()
    report = {"exported": len(done), "details": done,
              "skipped_already_text": len(skipped), "skipped_existing": len(existing),
              "failed": failed, "cancelled": cancelled,
              "out_dir": str(out), "seconds": round(time.time() - t0, 1)}
    return report
