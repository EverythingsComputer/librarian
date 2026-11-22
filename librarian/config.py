"""Config handling for librarian.

Config lives at <data_dir>/config.toml (default data_dir: ~/librarian).
Multiple library roots are allowed; each can be turned off (enabled = false)
without removing it from the index.
"""
from __future__ import annotations

import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DATA_DIR = Path(os.environ.get("LIBRARIAN_DATA", str(Path.home() / "librarian")))

# extensions we never try to read (binaries/media handled elsewhere)
SKIP_EXTS = {
    "exe", "msi", "dll", "so", "bin", "iso", "dmg", "deb", "rpm", "appimage",
    "mp4", "mov", "avi", "mkv", "wmv", "flv", "m4a", "mp3", "wav", "wma", "flac",
    "png", "jpg", "jpeg", "gif", "bmp", "ico", "tif", "tiff", "webp", "heic",
    "ttf", "otf", "woff", "woff2", "eot",
    "zip", "gz", "tar", "bz2", "xz", "7z", "rar", "cab",
    "pyc", "class", "pyo", "pyd",
}
# extensions the index understands
TEXT_EXTS = {"txt", "md", "markdown", "rst", "log", "csv", "tsv", "json", "html", "htm", "xml", "yaml", "yml"}
DOC_EXTS = {"pdf", "pptx", "docx", "msg"}
# media that we link to transcripts instead of indexing
MEDIA_EXTS = {"mp4", "mov", "mkv", "avi", "wmv"}


@dataclass
class LibraryRoot:
    path: Path
    name: str
    exclude: list[str] = field(default_factory=list)
    enabled: bool = True


@dataclass
class Config:
    data_dir: Path
    db_path: Path
    roots: list[LibraryRoot]
    embed_backend: str = "fastembed"
    embed_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embed_base_url: str = ""          # for backend="openai" (LMStudio etc.)
    embed_api_key: str = ""
    llm_base_url: str = ""            # OpenAI-compatible chat endpoint for doc summaries
    llm_model: str = ""
    llm_api_key: str = ""
    ocr: bool = False
    max_file_mb: int = 150
    index_archives: bool = False
    mcp_port: int = 8770              # default port for `librarian mcp-serve --transport http`
    mcp_token: str = ""               # bearer token; mcp-serve uses it when --token is omitted

    @classmethod
    def load(cls, data_dir: Path | None = None) -> "Config":
        ddir = Path(os.environ.get("LIBRARIAN_DATA", "")) if os.environ.get("LIBRARIAN_DATA") else (data_dir or DEFAULT_DATA_DIR)
        ddir = ddir.expanduser()
        cfg_path = ddir / "config.toml"
        if not cfg_path.exists():
            raise FileNotFoundError(
                f"No config at {cfg_path}. Run `librarian init` first, then edit config.toml."
            )
        raw = tomllib.loads(cfg_path.read_text())
        roots = []
        for r in raw.get("library", []):
            p = Path(r["path"]).expanduser()
            roots.append(LibraryRoot(path=p, name=r.get("name") or p.name,
                                     exclude=list(r.get("exclude", [])),
                                     enabled=bool(r.get("enabled", True))))
        idx = raw.get("index", {})
        emb = raw.get("embed", {})
        llm = raw.get("llm", {})
        srv = raw.get("server", {})
        return cls(
            data_dir=ddir,
            db_path=ddir / "index.db",
            roots=roots,
            embed_backend=emb.get("backend", "fastembed"),
            embed_model=emb.get("model", "sentence-transformers/all-MiniLM-L6-v2"),
            embed_base_url=emb.get("base_url", ""),
            embed_api_key=emb.get("api_key", ""),
            llm_base_url=llm.get("base_url", ""),
            llm_model=llm.get("model", ""),
            llm_api_key=llm.get("api_key", ""),
            ocr=bool(idx.get("ocr", False)),
            max_file_mb=int(idx.get("max_file_mb", 150)),
            index_archives=bool(idx.get("index_archives", False)),
            mcp_port=int(srv.get("mcp_port", 8770)),
            mcp_token=str(srv.get("mcp_token", "")),
        )

    @staticmethod
    def template() -> str:
        return """# Librarian config
# Add your document folders:  librarian add-folder /path/to/your/documents
# (or press "Add a folder" on the web page:  librarian ui)
#
# [[library]]                # one block per folder, or let the commands write them
# path = "/path/to/your/documents"
# name = "my-library"
# exclude = ["tmp", ".git"]  # folder-name fragments to skip

[index]
ocr = false            # true -> use ocrmypdf for PDFs with no text layer (needs: apt install ocrmypdf)
max_file_mb = 150      # skip anything larger than this
index_archives = false # true -> peek inside .zip files

[embed]
backend = "fastembed"  # "fastembed" (local ONNX, zero config) or "openai" (LMStudio / any OpenAI-compatible /v1/embeddings)
model = "sentence-transformers/all-MiniLM-L6-v2"
# base_url = "http://127.0.0.1:1234/v1"   # used when backend = "openai"
# model = "text-embedding-nomic-embed-text-v1.5"

[llm]                  # optional: powers nicer doc summaries (2-3 sentence cards)
# base_url = "http://127.0.0.1:1234/v1"
# model = "qwen/qwen3.8-27b"

[server]               # `librarian mcp-serve --transport http` defaults
# mcp_port = 8770
# mcp_token = "long-r…dom"   # required for non-loopback binds; shown in the web UI
"""


    @property
    def disabled_roots(self) -> list[str]:
        """Names of library roots the user switched off (hidden from search)."""
        return [r.name for r in self.roots if not r.enabled]


def set_server_value(cfg: Config, key: str, value: str) -> None:
    """Set key = "value" in the [server] section of config.toml, creating the
    section/line if missing. Used by the UI's token manager."""
    cfg_path = cfg.data_dir / "config.toml"
    text = cfg_path.read_text() if cfg_path.exists() else ""
    line_re = re.compile(rf'(?m)^{re.escape(key)} = .*$')
    new_line = f'{key} = "{value}"'
    if line_re.search(text):
        text = line_re.sub(new_line.replace("\\", "\\\\"), text, count=1)
    elif "[server]" in text:
        text = text.replace("[server]", f"[server]\n{new_line}", 1)
    else:
        text = text.rstrip("\n") + f"\n\n[server]\n{new_line}\n"
    cfg_path.write_text(text)
    try:
        cfg_path.chmod(0o600)      # it now holds a secret
    except OSError:
        pass


def ensure_data_dir(ddir: Path) -> None:
    ddir.mkdir(parents=True, exist_ok=True)


# ------------------------------------------------------------- config editing
# add/remove/toggle roots without touching the rest of config.toml: we locate
# the contiguous region spanning all [[library]] blocks, regenerate just that
# region, and keep every other line (including comments) byte-for-byte.

_LIB_HEADER = "[[library]]"


def _library_region(text: str) -> tuple[int, int]:
    """(start, end) char offsets of the whole [[library]] block region.
    Returns (-1, -1) when no blocks exist."""
    lines = text.splitlines(keepends=True)
    idx = [i for i, l in enumerate(lines) if l.strip() == _LIB_HEADER]
    if not idx:
        return -1, -1
    start = idx[0]
    last = idx[-1]
    for i in range(start, last):
        s = lines[i].strip()
        if s.startswith("[") and s != _LIB_HEADER:
            raise ValueError("config.toml has other sections mixed between [[library]] blocks; "
                             "move all library blocks together, then retry")
    end = last + 1
    # consume the block's key lines, then absorb trailing comments/blank lines
    # (e.g. the template's '# exclude = ...' hint) up to the next table header,
    # so a rewrite can't orphan them under the wrong block
    for i in range(end, len(lines)):
        s = lines[i].strip()
        if s.startswith("["):
            break
        end = i + 1
    return (sum(map(len, lines[:start])), sum(map(len, lines[:end])))


def _render_roots(roots: list[LibraryRoot]) -> str:
    out = []
    for r in roots:
        out.append(f"{_LIB_HEADER}\npath = \"{r.path}\"\nname = \"{r.name}\"\n")
        if r.exclude:
            items = ", ".join(f'"{e}"' for e in r.exclude)
            out.append(f"exclude = [{items}]\n")
        if not r.enabled:
            out.append("enabled = false   # switched off: kept in the index, hidden from search\n")
        out.append("\n")
    return "".join(out).rstrip("\n") + "\n"


def write_roots(cfg: Config, roots: list[LibraryRoot]) -> None:
    cfg_path = cfg.data_dir / "config.toml"
    text = cfg_path.read_text() if cfg_path.exists() else ""
    start, end = _library_region(text)
    block = _render_roots(roots)
    if start < 0:
        # no library section yet: put it right after leading comments / before first table
        m = re.search(r"(?m)^\[(?!\[library\])", text)
        pos = m.start() if m else len(text)
        text = text[:pos] + "# Librarian library folders (toggle with: librarian disable <name>)\n" \
               + block + "\n" + text[pos:]
    else:
        # _render_roots already ends with one \n; add exactly one blank line,
        # and drop old trailing blanks so repeated add/enable/disable writes
        # don't accumulate whitespace (idempotent)
        text = text[:start] + block + "\n" + text[end:].lstrip("\n")
    cfg_path.write_text(text)
    cfg.roots = roots


def add_root(cfg: Config, path: str, name: str | None = None) -> LibraryRoot:
    p = Path(path).expanduser()
    if not p.is_dir():
        raise ValueError(f"not a directory: {p}")
    nm = (name or p.name).strip() or p.name
    existing = {r.name for r in cfg.roots}
    if nm in existing:
        i = 2
        while f"{nm}-{i}" in existing:
            i += 1
        nm = f"{nm}-{i}"
    root = LibraryRoot(path=p, name=nm)
    write_roots(cfg, [*cfg.roots, root])
    return root


def remove_root(cfg: Config, name: str) -> LibraryRoot:
    root = find_root(cfg, name)
    write_roots(cfg, [r for r in cfg.roots if r.name != root.name])
    return root


def set_root_enabled(cfg: Config, name: str, enabled: bool) -> LibraryRoot:
    root = find_root(cfg, name)
    root.enabled = enabled
    write_roots(cfg, cfg.roots)
    return root


def find_root(cfg: Config, name: str) -> LibraryRoot:
    low = name.strip().lower()
    for r in cfg.roots:
        if r.name.lower() == low or str(r.path).lower() == low or r.path.name.lower() == low:
            return r
    known = ", ".join(r.name for r in cfg.roots) or "(none)"
    raise ValueError(f"no library named '{name}'. Known libraries: {known}")
