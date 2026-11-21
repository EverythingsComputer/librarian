"""Structure-aware chunking: pages/slides are hard boundaries; long units split
with overlap; short units merge forward. Chunk labels preserved for citations."""
from __future__ import annotations

import re

CHUNK_MAX = 1100      # chars
CHUNK_MIN = 200       # merge units smaller than this into the next
OVERLAP = 120
SENT_SPLIT = re.compile(r"(?<=[.!?:;])\s+")


def chunk_units(units: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """[(label, text)] -> [(label, chunk_text)]"""
    # 1. merge tiny units into previous/next
    merged: list[tuple[str, str]] = []
    for label, text in units:
        text = text.strip()
        if not text:
            continue
        if merged and len(text) < CHUNK_MIN and len(merged[-1][1]) + len(text) <= CHUNK_MAX:
            pl, pt = merged[-1]
            merged[-1] = (pl, pt + "\n" + text)
        elif merged and len(merged[-1][1]) < CHUNK_MIN:
            pl, pt = merged[-1]
            merged[-1] = (pl, pt + "\n" + text)
        else:
            merged.append((label, text))
    # 2. split oversized units with overlap, keeping the label
    out: list[tuple[str, str]] = []
    for label, text in merged:
        if len(text) <= CHUNK_MAX:
            out.append((label, text))
            continue
        pieces = _split_with_overlap(text)
        for i, p in enumerate(pieces):
            out.append((label if len(pieces) == 1 else f"{label} (cont {i+1})", p))
    return out


def _split_with_overlap(text: str) -> list[str]:
    sentences: list[str] = []
    for para in text.split("\n"):
        para = para.strip()
        if not para:
            continue
        sentences.extend(s for s in SENT_SPLIT.split(para) if s.strip())
    pieces, buf = [], ""
    for s in sentences:
        while len(s) > CHUNK_MAX:  # pathological long sentence
            if buf:
                pieces.append(buf.strip())
                buf = ""
            pieces.append(s[:CHUNK_MAX])
            s = s[CHUNK_MAX - OVERLAP:]
        if len(buf) + len(s) + 1 > CHUNK_MAX and buf:
            pieces.append(buf.strip())
            buf = _tail_overlap(buf)
        buf = (buf + " " + s).strip() if buf else s
    if buf.strip():
        pieces.append(buf.strip())
    return pieces


def _tail_overlap(buf: str) -> str:
    tail = buf[-OVERLAP:]
    m = re.search(r"[.!?:;]\s", tail)
    return tail[m.end():].strip() if m else tail.strip()


def embed_text(title: str, folder: str, label: str, text: str) -> str:
    """Prefix context onto the text that goes into the embedding model."""
    head = " — ".join(x for x in (title, folder, label) if x)
    return f"{head}: {text}"[:2000] if head else text[:2000]
