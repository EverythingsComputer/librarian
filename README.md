# Librarian

Search your own document folders on your own computer.

Librarian reads PDF, Word, PowerPoint, text, and Outlook `.msg` email files and
builds one local index. Then you can search it by exact words, by meaning, or
both at once. You can also connect an AI assistant to the index so it can find
and quote the documents that answer a question.

Everything stays on your machine. No uploads, no accounts, no cloud service —
Librarian makes no network calls except a one-time download of a small search
model, and any service you choose to add yourself.

Unlike "chat with your PDFs" demos that show only the few closest matches,
Librarian can list **every** document that mentions a topic, with exact word
counts, and every result names its document and page (or slide). If something
relevant exists, you see that it exists.

## Quick start

```bash
./install.sh     # asks for your password once, to install two system packages
librarian ui     # opens the control page at http://127.0.0.1:8765
```

On the page: **Add a folder** → pick where your documents live → it indexes
automatically → type a question in the search box.

Without the web page, the same steps are:

```bash
librarian add-folder ~/Documents/MyReports
librarian sync
librarian search "what did we decide about the roof repair"
```

## Requirements

- Linux (written and tested on Ubuntu 24.04+, x86_64), Python 3.10+
- `poppler-utils` (the installer takes care of it)
- About 1.5 GB free disk the first time (a small search model downloads once)
- Older 4-core mini PCs and laptops are enough; no graphics card needed.
  Indexing uses about 1–1.5 GB of memory; searching uses much less.

## Folders and privacy switches

Your library is just a list of ordinary folders — add as many as you like.
Any folder can be **switched off**: its documents instantly disappear from all
search results, the web page, and anything an AI assistant asks, without
deleting or un-indexing anything. Switching it back on is equally instant.

```bash
librarian folders                      # list folders and their on/off state
librarian disable "Taxes 2024"         # hide one folder
librarian enable  "Taxes 2024"         # show it again
librarian remove-folder "Taxes 2024"   # stop managing it (your files are untouched)
```

Daily upkeep is one command, or one button on the web page:

```bash
librarian sync     # notice files you added or deleted; index the new ones
```

## Connecting an AI assistant (optional)

Librarian speaks MCP, the standard way AI tools attach to outside services.
Claude Desktop, LMStudio, and many other tools understand it.

**On the same computer:** give your AI tool this command:

```
command:  ~/librarian-app/.venv/bin/python
args:     ["-m", "librarian.mcp_server"]
```

**From other computers** (e.g. over [Tailscale](https://tailscale.com), a free
private network): start the server once, then clients use its address:

```bash
librarian mcp-serve --transport http --host <this-machine-ip> --port 8770
```

Clients connect to `http://<this-machine-ip>:8770/mcp` and authenticate with
the access token — either as an `Authorization: Bearer <token>` header or, for
clients that accept only a plain URL, appended as `?token=<token>`. The token
lives in the settings file and can be shown, changed, or regenerated from the
web page.

Security notes, in plain terms: the web page is reachable only from the
machine it runs on, by design, because it can display the token. The MCP
server is the only part that accepts network connections; it refuses to start
on a network address without a token, and that one shared token is the only
thing standing between the network and your documents. Keep it on a private
network. Do not expose it to the open internet without a proper HTTPS proxy
with its own protections in front.

## Settings

One plain-text file: `~/librarian/config.toml`. The defaults work as-is.
Things you might change:

- library folder paths, and sub-folder names to skip (`exclude`)
- a local AI model for one-line document summaries (off by default)
- if you change the search model, rebuild:
  `rm ~/librarian/index.db* && librarian build`

The index is disposable — everything is derived from your files. Moving to a
new machine: install there, copy the settings file (not the index), run
`librarian build`.

## Command reference

| Command | What it does |
|---|---|
| `librarian ui` | local web page: search, sync, folders, token |
| `librarian sync` | index new files, forget deleted ones |
| `librarian search "words"` | combined word + meaning search |
| `librarian coverage "topic"` | every document touching a topic, with counts |
| `librarian find "exact phrase"` | which documents contain that phrase, how often |
| `librarian multi "a" "b" "c"` | several phrasings of one question at once |
| `librarian card 42` / `read 42` | a document's summary, or its full text |
| `librarian related 42` | documents closest to number 42 in meaning |
| `librarian status` | index size and state |
| `--json` on most commands | machine-readable output for scripts |

Nice touches for real libraries: duplicate copies of a document are detected
and grouped; a text transcript saved next to a video makes the video
searchable; PDFs with no readable text are reported, never silently skipped.

## Known limitations

- **Scanned PDFs** (photos of pages) are not searchable by default. Install
  `ocrmypdf` and set `ocr = true` in the settings file to read them — much
  slower, only if you need it.
- Files over 150 MB are skipped. The contents of zip archives are skipped
  unless enabled in settings.
- Meaning-based search scans the whole index exactly on every query: fast up
  to roughly 200,000 indexed passages (a few thousand typical documents),
  around a second at a million. Word-based search stays fast at any size and
  works even without the search model.
- Linux only for now. The pieces are standard Python, but Windows and macOS
  have not been tested.
- `.msg` emails that are encrypted or signed-only may yield no text.
- The networked MCP server has one shared token: no per-device keys, no rate
  limiting, no built-in HTTPS (see security notes above).

## License

MIT — see LICENSE.
