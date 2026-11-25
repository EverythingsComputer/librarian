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

**Easiest:** download `Librarian-<version>-x86_64.AppImage` from this repo's
[Releases](../../releases) page. It is a single file — nothing to install:

```bash
chmod +x Librarian-*-x86_64.AppImage
./Librarian-*-x86_64.AppImage      # opens the control page in your browser
```

(If double-clicking does nothing, install FUSE once: `sudo apt install libfuse2`,
or run it from a terminal as above.)

**From source** instead:

```bash
./install.sh     # asks for your password once, to install two system packages
librarian ui     # opens the control page at http://127.0.0.1:8765
```

On the page: **Add a folder** → pick where your documents live → it indexes
automatically → type a question in the search box.

That's a complete setup. To let an AI assistant use the same library — from
this machine or another one — follow the three steps in
[Connecting an AI assistant](#connecting-an-ai-assistant-optional) below.

Without the web page, the same steps are:

```bash
librarian add-folder ~/Documents/MyReports
librarian sync
librarian search "what did we decide about the roof repair"
```

## Requirements

- Linux (written and tested on Ubuntu 24.04+, x86_64), Python 3.10+
- `poppler-utils` + `tesseract-ocr` for source installs — the **AppImage
  needs none of this**; it ships its own PDF extractor and OCR engine (v0.2.7+)
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

Scanned PDFs: tick **OCR scanned PDFs** next to the update button (or run
`librarian sync --ocr`, or set `ocr = true` in the settings file). Only files
with no text layer are OCR'd, their originals are never modified, and files
already OCR'd are not re-done. The AppImage bundles the OCR engine — no
install needed; source installs need `tesseract-ocr` (install.sh does it).

Honesty guarantee (v0.2.5): "searchable" counts only files that actually
produced text. If anything failed to extract, the page says how many and why,
with a **Retry textless files** button; `sync --retry-empty` is the CLI form.
Upgrading the app automatically re-extracts files that failed because an
extraction tool was missing — no manual rebuild.

## Connecting an AI assistant (optional)

Librarian speaks MCP, the standard way AI tools attach to outside services.
Claude Desktop, LMStudio, Unsloth, Open WebUI, and many other tools
understand it.

**Same computer, source install (stdio):** give your AI tool this command:

```
command:  ~/librarian-app/.venv/bin/python
args:     ["-m", "librarian.mcp_server"]
```

**AppImage — or any assistant on another computer (over the network):**
three steps.

1. **Start the server** on the machine that has the documents. Open a
   terminal in the folder containing the AppImage and run:

   ```bash
   ./Librarian-*-x86_64.AppImage mcp-serve --transport http \
       --host <this-machine-ip> --port 8770
   ```

   (Source installs: `librarian mcp-serve --transport http --host <ip>
   --port 8770`.) The same command — with the real paths and this
   machine's address already filled in — appears in the web page's
   "Connect an agent" section; copy and paste it there instead if you
   prefer (v0.2.4+).

   The server takes its access token from the settings file automatically
   (pass `--token <token>` to override). It refuses to start on a network
   address without a token. It runs until you press Ctrl-C — keep it up
   with tmux or a systemd service if you want it always on.

2. **Get the token:** on the web page, "Connect an agent" shows the access
   token; you can also set or regenerate one there.

3. **Point the assistant at the server.** In the client's MCP settings,
   use this URL:

   ```
   http://<this-machine-ip>:8770/mcp?token=<token>
   ```

   If the client can send a header instead, drop the `?token=` part and
   set `Authorization: Bearer <token>`.

Over [Tailscale](https://tailscale.com) (a free private network),
`<this-machine-ip>` is the machine's `100.x.y.z` address — the web page
shows it under "This machine on Tailscale".

## Sharing parts of your library (profiles)

The token above is the **admin** token: full access to every folder. To give
a friend, colleague, or another agent access to only *some* of your library,
create a **profile**: a named, per-person token that can only see the folders
you check.

- Web page → **Shared access** → name the profile, tick the folders →
  **Create**. The token is shown **once** — only a hash is stored, so if it's
  lost, press "new token" (the old one dies immediately).
- Or from a terminal:

  ```bash
  librarian profile add friend-1 --roots "Chemistry,Historical"   # prints the token once
  librarian profile list
  librarian profile roots friend-1 --set "Chemistry"   # change what it sees
  librarian profile token friend-1                     # reset (old token dies now)
  librarian profile disable friend-1                   # block without deleting
  librarian profile rm friend-1                        # revoke for good
  ```

The recipient uses the **same URL** as step 3, with their own token. They see
only their granted folders — in search results, counts, the folder map, and
by document ID. Everything else is invisible, not merely filtered. Profiles
are **read-only**: the one mutating tool (`update_index`) is admin-only and
hidden from a profile's tool list entirely. Changes apply to the running
server immediately — no restart, and other clients' tokens are unaffected.

Security notes, in plain terms: the web page is reachable only from the
machine it runs on, by design, because it can display the admin token and
manage profiles. The MCP server is the only part that accepts network
connections; it refuses to start on a network address without a credential,
and every request must present either the admin token or a valid profile
token. Tokens travel in plain HTTP, so keep this on a private network
(Tailscale) or behind an HTTPS proxy with its own protections in front —
a token is then equivalent to a password you cannot see once handed out.

## Who may download the actual files

Reading and downloading are separate decisions. A profile can always ask
its agent to search and read extracted text (`search`, `read_doc`, …) —
that is what the index is for. Getting the **original file** out is gated
by two switches, in strict hierarchy:

1. **The folder's ⤓ switch** (Library folders, the small second toggle) —
   master, wins over everything: off means **no profile** can download
   anything from that folder, whatever its own grant says. Some folders
   you may simply never want handed out.
2. **The profile's ⤓ button** (Shared access) — only consulted when the
   folder allows: `⤓ downloads on` hands out original files,
   `⤓ text only` (the default for every new profile) does not.

The admin token — yours, the one in the settings file — can always
download any file of its own library, and the web page's own search
results carry a ⤓ file link for you.

An agent uses this natively: `file_url(doc_id)` returns a download URL
(or a clear refusal explaining which switch blocked it), and
`GET /file/<doc_id>` on the same MCP port with the same token streams the
original bytes. No extra server, nothing to set up. From a terminal:

```bash
librarian profile files friend-1            # grant raw-file downloads
librarian profile files friend-1 --off      # back to text-only
librarian folder-files Chemistry --off      # master block: nobody downloads
librarian folder-files Chemistry            # allow again (per profile grant)
```

## Searchable copies (export)

Indexing never writes into your files: when Librarian OCRs a scanned PDF,
the text lands in the index, not in the PDF. If you need a scanned
document to *travel* with its OCR — to a tablet, another machine, a
colleague — export a **searchable copy**:

- Web page → **Searchable copies** → whole library or selected folders →
  **Export copies**. Progress shows per file; the page can close, the run
  continues; Cancel stops after the current file.
- Or from a terminal:

  ```bash
  librarian export --all                          # every folder
  librarian export --folders "Chemistry,History"  # chosen folders
  librarian export --docs 12,37,88                # picked documents
  librarian export --all --out /mnt/usb/scanned   # custom destination
  ```

For each scanned (zero-text) PDF this writes `<name>.searchable.pdf`
into the destination folder, with a real invisible text layer baked
in: selectable and searchable in any PDF reader, visually identical to
the original. Originals are never
modified (byte-for-byte), files that already have a text layer are left
alone, an existing copy is never overwritten, and a cancelled run
resumes where it stopped. Export is an owner action, available from the
loopback web page or the terminal on the server — profiles never get it.

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
| `librarian ui` | local web page: search, sync, folders, tokens, shared access |
| `librarian sync` | index new files, forget deleted ones |
| `librarian search "words"` | combined word + meaning search |
| `librarian coverage "topic"` | every document touching a topic, with counts |
| `librarian find "exact phrase"` | which documents contain that phrase, how often |
| `librarian multi "a" "b" "c"` | several phrasings of one question at once |
| `librarian card 42` / `read 42` | a document's summary, or its full text |
| `librarian related 42` | documents closest to number 42 in meaning |
| `librarian status` | index size and state |
| `librarian export --all` / `--folders` / `--docs` | searchable PDF copies (originals untouched) |
| `librarian profile files NAME [--off]` | may that profile download original files? |
| `librarian folder-files NAME [--off]` | master download block for a whole folder |
| `--json` on most commands | machine-readable output for scripts |

Nice touches for real libraries: duplicate copies of a document are detected
and grouped; a text transcript saved next to a video makes the video
searchable; PDFs with no readable text are reported, never silently skipped.

## Known limitations

- **Scanned PDFs** (photos of pages) are read via OCR when enabled
  (`ocr = true`, or the switch on the web page). The AppImage bundles
  OCR tools; from source install poppler-utils + tesseract-ocr. OCR
  stays in the index — to put it *in* a file, export a searchable copy.
- Files over 150 MB are skipped. The contents of zip archives are skipped
  unless enabled in settings.
- Meaning-based search scans the whole index exactly on every query: fast up
  to roughly 200,000 indexed passages (a few thousand typical documents),
  around a second at a million. Word-based search stays fast at any size and
  works even without the search model.
- Linux only for now. The pieces are standard Python, but Windows and macOS
  have not been tested.
- `.msg` emails that are encrypted or signed-only may yield no text.
- The networked MCP server authenticates bearer tokens (admin + per-profile,
  v0.3.0): there is no rate limiting, no user accounts behind the tokens, and
  no built-in HTTPS (see security notes above). A token is a bearer secret —
  whoever holds it holds the access. File downloads (v0.4.0) obey the
  folder/profile ⤓ hierarchy, but note that a profile with text access can
  always quote the *extracted text* its agent is allowed to read.

## License

MIT — see LICENSE.
