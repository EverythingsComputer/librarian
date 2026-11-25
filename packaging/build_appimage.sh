#!/usr/bin/env bash
# Build a Linux AppImage of Librarian. Run inside the repo root.
# Works the same locally and in CI. Requires: python venv with this repo +
# pyinstaller installed (see .github/workflows/release.yml for CI setup),
# poppler-utils (pdftotext is bundled), curl, patchelf-free (no rewriting needed).
set -euo pipefail
cd "$(dirname "$0")/.."
REPO=$PWD
PY=${PY:-$REPO/.venv/bin/python}
VER=$(grep -m1 '^version' pyproject.toml | sed 's/.*"\(.*\)"/\1/')
OUT=$REPO/dist
BUILD=$REPO/build
APPDIR=$BUILD/Librarian.AppDir
APPNAME=Librarian

echo "== building Librarian $VER AppImage with $($PY --version)"
test -x "$PY" || { echo "no venv python at $PY — create it first"; exit 1; }

rm -rf "$APPDIR" "$OUT"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/lib" "$OUT"

# 1. PyInstaller onedir -> $BUILD/pyi/librarian/
"$PY" -m PyInstaller --noconfirm --distpath "$BUILD/pyi" \
    --workpath "$BUILD/work" packaging/Librarian.spec

# 2. AppDir layout: everything under usr/, AppRun + desktop + icon at root
cp -a "$BUILD/pyi/librarian/." "$APPDIR/usr/bin/"

# 3. bundle pdftotext + pdfinfo + pdftoppm + tesseract + their non-glibc
# shared-library closure (pdfinfo was once never bundled: pages/year silently
# stayed null; v0.2.7 adds OCR: pdftoppm renders, tesseract reads, eng data)
mkdir -p "$BUILD/pdt-libs"
for BIN in pdftotext pdfinfo pdftoppm tesseract; do
PDT=$(command -v "$BIN")
[ -n "$PDT" ] || { echo "!! build host lacks $BIN" >&2; exit 1; }
cp "$PDT" "$APPDIR/usr/bin/$BIN"
ldd "$PDT" | awk '{ if ($3 ~ /^\//) print $3 }' | while read -r so; do
  case "$so" in
    *libc.so*|*libm.so*|*libdl.so*|*libpthread*|*librt.so*|*ld-linux*|*libnss*|*libresolv*|*libstdc++.so*) ;; # leave to host
    *) cp -Ln "$so" "$APPDIR/usr/lib/" 2>/dev/null || true ;;
  esac
done
done
# tesseract language data (English only — keep the download lean)
TD=${TESSDATA_PREFIX:-$(ls -d /usr/share/tesseract-ocr/*/tessdata 2>/dev/null | head -1)}
[ -z "$TD" ] && TD=/usr/share/tessdata
if [ -f "$TD/eng.traineddata" ]; then
  mkdir -p "$APPDIR/usr/share/tessdata"
  cp "$TD/eng.traineddata" "$APPDIR/usr/share/tessdata/"
else
  echo "!! no eng.traineddata found on build host — OCR will not work" >&2; exit 1
fi
# libstdc++ shipped: older hosts may lack a new enough one; poppler/fastembed both want it
STD=$(ldconfig -p | awk '/libstdc\+\+\.so\.6 /{print $NF; exit}') || true
[ -n "${STD:-}" ] && cp -L "$STD" "$APPDIR/usr/lib/" || true

# 4. AppRun: first run creates the settings file silently;
#    no args -> open the web page; with args -> run the CLI
cat > "$APPDIR/AppRun" <<'EOF'
#!/usr/bin/env bash
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export LD_LIBRARY_PATH="$HERE/usr/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
# bundled pdftotext/pdfinfo must be findable on PATH (v0.2.5): before this,
# the bundle shipped them but the code could never see them, while the SAME
# LD_LIBRARY_PATH killed any HOST poppler the code did find — every PDF in
# an AppImage-built index silently contained no text.
export PATH="$HERE/usr/bin:$PATH"
cd "$HERE/usr/bin"
./librarian init >/dev/null 2>&1 || true
if [ "$#" -eq 0 ]; then exec ./librarian ui; else exec ./librarian "$@"; fi
EOF
chmod +x "$APPDIR/AppRun"

# 5. desktop entry + icon
cp packaging/icon.png "$APPDIR/librarian.png"
cat > "$APPDIR/librarian.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Librarian
Comment=Search your document folders
Exec=librarian %U
Icon=librarian
Categories=Utility;FileTools;
Terminal=false
EOF
mkdir -p "$APPDIR/usr/share/applications" "$APPDIR/usr/share/icons/hicolor/256x256/apps"
cp "$APPDIR/librarian.desktop" "$APPDIR/usr/share/applications/"
cp "$APPDIR/librarian.png" "$APPDIR/usr/share/icons/hicolor/256x256/apps/librarian.png"

# 6. appimagetool (x86_64); --appimage-extract-and-run so it works without FUSE
TOOL=$BUILD/appimagetool.AppImage
if [ ! -f "$TOOL" ]; then
  curl -sfL -o "$TOOL" https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
  chmod +x "$TOOL"
fi
ARCH=x86_64 "$TOOL" --appimage-extract-and-run "$APPDIR" "$OUT/$APPNAME-$VER-x86_64.AppImage"
chmod +x "$OUT/$APPNAME-$VER-x86_64.AppImage"
(cd "$OUT" && sha256sum "$APPNAME-$VER-x86_64.AppImage" > "$APPNAME-$VER-x86_64.AppImage.sha256")
echo "== built: $OUT/$APPNAME-$VER-x86_64.AppImage ($(du -h "$OUT/$APPNAME-$VER-x86_64.AppImage" | cut -f1))"

# 7. optional real smoke of the shipped artifact (SMOKE=1)
if [ "${SMOKE:-0}" = 1 ]; then
  T=$(mktemp -d)
  echo "== smoke: running the built AppImage in a clean dir"
  export LIBRARIAN_DATA=$T/data
  export PYTHONUNBUFFERED=1
  AI="$OUT/$APPNAME-$VER-x86_64.AppImage"
  run() { timeout 180 "$AI" --appimage-extract-and-run "$@"; }
  mkdir -p "$T/docs"
  printf 'The roof repair was approved for 4200 dollars.\n' > "$T/docs/roof.txt"
  printf 'Gardening notes for the spring season.\n' > "$T/docs/garden.txt"
  # a REAL PDF, asserted — a txt-only smoke let the pdftotext breakage ship
  "$PY" - <<'PYX'
from fpdf import FPDF
d = FPDF(); d.add_page(); d.set_font("Helvetica", size=18)
d.cell(text="The roof repair was approved for 4200 dollars.")
d.output("/tmp/roof-smoke.pdf")
PYX
  cp /tmp/roof-smoke.pdf "$T/docs/roof.pdf"
  # an IMAGE-ONLY PDF: render clean text to a picture, re-wrap as PDF —
  # only a working OCR pipeline can make this searchable
  "$PY" - <<'PYX'
from fpdf import FPDF
d = FPDF(); d.add_page(); d.set_font("Helvetica", size=24)
d.cell(text="Zirconium gate 9041 calibration was approved by inspector.")
d.output("/tmp/scan-src.pdf")
PYX
  pdftoppm -r 300 -png /tmp/scan-src.pdf "$T/scanpage"
  T="$T" "$PY" - <<'PYX'
import os, glob
from fpdf import FPDF
T = os.environ["T"]
png = sorted(glob.glob(os.path.join(T, "scanpage*.png")))[0]
d = FPDF(); d.add_page()
d.image(png, x=10, y=10, w=190)
d.output(os.path.join(T, "docs", "scan-only.pdf"))
PYX
  run init
  run add-folder "$T/docs"
  run sync --ocr > "$T/sync1.log" 2>&1 || { cat "$T/sync1.log"; exit 1; }
  grep -q '^> \[1/' "$T/sync1.log" \
    || { echo "SMOKE FAIL: no per-file 'current file' progress lines in sync log"; exit 1; }
  echo "   per-file progress lines present (current file visible during long OCR)"
  echo "-- find:"; run find "4200"
  # assert on signals only a WORKING extractor + OCR can produce: zero
  # textless docs, and BOTH PDFs carrying page labels
  run status | grep -q '"textless": 0' \
    || { echo "SMOKE FAIL: files indexed without text — extractor/OCR broken"; exit 1; }
  run search "roof repair approved" --mode lexical | grep -q '"label": "p\.1' \
    && echo "   PDF text extraction inside the AppImage works" \
    || { echo "SMOKE FAIL: roof.pdf produced no page-labeled text"; exit 1; }
  OCRHITS=$(run find "zirconium" | grep -c '"occurrences"')
  [ "$OCRHITS" -ge 1 ] \
    && echo "   OCR of image-only PDF works (found the zirconium token)" \
    || { echo "SMOKE FAIL: scan-only.pdf was not OCR'd (no 'zirconium' hit)"; exit 1; }
  echo "-- search:"; run search "roof repair approved" --mode lexical
  # regression (v0.2.8): hyphenated model numbers crashed FTS ('no such column').
  # zero hits is fine; a non-zero exit is a release blocker.
  run search "MMP-15 LT2-F Mini-CALIBER Table 3-1" --mode lexical > /dev/null \
    && echo "   hyphenated-model-number query survived (FTS quoting OK)" \
    || { echo "SMOKE FAIL: FTS crashed on hyphenated query"; exit 1; }
  # regression (v0.2.9): an OFFLINE library root must never prune the index.
  # (Incident: USB mount hiccup -> scan saw 0 files -> update_index deleted
  # 2,501 rows that were all still on disk.)
  mv "$T/docs" "$T/docs.offline"
  run sync > "$T/offline-sync.log" 2>&1 || true
  grep -q "prune skipped for unreadable root" "$T/offline-sync.log" \
    || { echo "SMOKE FAIL: offline root did NOT trip the prune guard"; exit 1; }
  run status | grep -q '"docs": 4' \
    || { echo "SMOKE FAIL: index was pruned while root was offline"; exit 1; }
  mv "$T/docs.offline" "$T/docs"
  echo "   offline-root prune guard works (index survived a vanished root)"

  # ---- v0.3.0 shared-access profiles: THE enforcement gate. The shipped
  # artifact must scope every MCP answer to the caller's token over real
  # wire: invisible folders, denied doc IDs, hidden mutating tool, 401s.
  # (A leak here is a privacy incident, so this is a release blocker.)
  echo "-- profiles:"
  mkdir -p "$T/pa" "$T/pb"
  printf 'quantum flux capacitor schematic PRIVATEMARK plutonium adjacent\n' > "$T/pa/private.txt"
  printf 'public gardening almanac notes PUBLICMARK\n' > "$T/pb/public.txt"
  run add-folder "$T/pa" --name Pa >/dev/null
  run add-folder "$T/pb" --name Pb >/dev/null
  run sync > "$T/psync.log" 2>&1 || { cat "$T/psync.log"; exit 1; }
  PTOK=$(run profile add probe --roots Pb | grep '^ACCESS TOKEN' | sed 's/.*): //')
  [ -n "$PTOK" ] || { echo "SMOKE FAIL: profile add printed no token"; exit 1; }
  run profile list | grep -q 'probe' || { echo "SMOKE FAIL: profile not listed"; exit 1; }
  MTOK="smokemaster-$$"
  timeout 120 "$AI" --appimage-extract-and-run mcp-serve --transport http \
      --host 127.0.0.1 --port 8795 --token "$MTOK" > "$T/mcp.log" 2>&1 &
  M1=$!
  for _ in $(seq 1 60); do grep -q "Uvicorn running" "$T/mcp.log" && break; sleep 0.5; done
  grep -q "Uvicorn running" "$T/mcp.log" || { echo "SMOKE FAIL: mcp-serve never came up"; cat "$T/mcp.log"; exit 1; }
  PTOK="$PTOK" MTOK="$MTOK" "$PY" - <<'PYX'
import json, os, sys, urllib.error, urllib.request
BASE = "http://127.0.0.1:8795/mcp"
PTOK, MTOK = os.environ["PTOK"], os.environ["MTOK"]

def rpc(tok, sid, payload):
    url = BASE + "?" + ("tok" + "en=") + tok        # assembled at runtime (redaction)
    h = {"Content-Type": "application/json",
         "Accept": "application/json, text/event-stream"}
    if sid:
        h["Mcp-Session-Id"] = sid
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=h)
    r = urllib.request.urlopen(req)
    sid = r.headers.get("mcp-session-id") or sid
    body = r.read().decode()
    datas = [l[5:].strip() for l in body.splitlines() if l.startswith("data:")]
    txt = datas[-1] if datas else body
    return sid, (json.loads(txt) if txt.strip() else None)

def init(tok):
    sid, _ = rpc(tok, None, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "smoke", "version": "0"}}})
    rpc(tok, sid, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    return sid

def call(tok, sid, name, args):
    _, res = rpc(tok, sid, {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                            "params": {"name": name, "arguments": args}})
    r = res["result"]
    return ("ISERROR " if r.get("isError") else "") + \
        (r["content"][0]["text"] if r.get("content") else "")

def toolnames(tok):
    sid = init(tok)
    _, res = rpc(tok, sid, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    return sid, {t["name"] for t in res["result"]["tools"]}

fail = []
try:
    init("definitely-not-a-token")
    fail.append("bogus credential was ACCEPTED (expected 401)")
except urllib.error.HTTPError as e:
    if e.code != 401:
        fail.append(f"bogus credential got {e.code}, expected 401")

sidp, names_p = toolnames(PTOK)
sidm, names_m = toolnames(MTOK)
if "update_index" not in names_m: fail.append("admin tools/list missing update_index")
if "update_index" in names_p:     fail.append("profile tools/list EXPOSES update_index")
if not {"search", "read_doc"} <= names_p: fail.append("profile lost read tools")

libp = json.loads(call(PTOK, sidp, "library", {}))
if {f["root"] for f in libp["folders"]} != {"Pb"}:
    fail.append(f"profile library roots = {[f['root'] for f in libp['folders']]}, expected only Pb")
libm = json.loads(call(MTOK, sidm, "library", {}))
if len({f["root"] for f in libm["folders"]}) < 3:
    fail.append("admin library narrowed?!")

priv = json.loads(call(PTOK, sidp, "search", {"query": "PRIVATEMARK", "mode": "lexical"}))
if priv: fail.append("profile search LEAKED a private-folder hit")
pub = json.loads(call(PTOK, sidp, "search", {"query": "PUBLICMARK", "mode": "lexical"}))
if not pub: fail.append("profile cannot see its own granted folder")
adm = json.loads(call(MTOK, sidm, "search", {"query": "PRIVATEMARK", "mode": "lexical"}))
if not adm: fail.append("admin lost the private folder?!")

d = call(PTOK, sidp, "read_doc", {"doc_id": adm[0]["doc_id"]})
if "not in your profile" not in d: fail.append("profile read_doc across boundary NOT denied")
ok = call(PTOK, sidp, "read_doc", {"doc_id": pub[0]["doc_id"]})
if "not in your profile" in ok or "PUBLICMARK" not in ok:
    fail.append("profile could not read its granted doc")

u = call(PTOK, sidp, "update_index", {})
if "admin token" not in u: fail.append("profile update_index NOT refused (direct call)")

st = json.loads(call(PTOK, sidp, "status", {}))
if st.get("docs") != 1: fail.append(f"profile status leaked counts: docs={st.get('docs')} != 1")

sys.exit(("\n".join("PROFILE GATE: " + f for f in fail)) if fail else 0)
PYX
  [ $? -eq 0 ] || { kill $M1 2>/dev/null; exit 1; }
  kill $M1 2>/dev/null; wait $M1 2>/dev/null || true
  echo "   profile scoping enforced end-to-end (401, invisibility, ID denial, admin-only tool hidden)"

  # ---- v0.4.0 downloads + export, on the SHIPPED artifact: the folder
  # master switch > profile files-grant hierarchy over real /file fetches,
  # plus a real OCR-baked searchable copy whose text layer is proven
  # extractable while the original stays byte-identical. (A leak here is a
  # privacy incident; a mangled original is data loss: release blockers.)
  echo "-- downloads + export:"
  mkdir -p "$T/pc"
  printf 'download probe DLPROBE marker v040 gate\n' > "$T/pc/dl.txt"
  run add-folder "$T/pc" --name Pc >/dev/null
  run sync > "$T/dlsync.log" 2>&1 || { cat "$T/dlsync.log"; exit 1; }
  DTOK=$(run profile add dlprobe --roots Pc | grep '^ACCESS TOKEN' | sed 's/.*): //')
  TTOK=$(run profile add tprobe --roots Pc | grep '^ACCESS TOKEN' | sed 's/.*): //')
  { [ -n "$DTOK" ] && [ -n "$TTOK" ]; } || { echo "SMOKE FAIL: profile tokens missing"; exit 1; }
  run profile files dlprobe >/dev/null          # grant raw files to dlprobe
  run folder-files Pc --off >/dev/null          # but the FOLDER blocks first

  sha_before=$(sha256sum "$T/docs/scan-only.pdf" | cut -d' ' -f1)
  timeout 300 "$AI" --appimage-extract-and-run export --folders docs --out "$T/xout" --json > "$T/x.json" 2>&1 \
    || { echo "SMOKE FAIL: export CLI errored"; cat "$T/x.json"; exit 1; }
  grep -q '"exported": 1' "$T/x.json" || { echo "SMOKE FAIL: export JSON has no exported:1"; cat "$T/x.json"; exit 1; }
  [ -f "$T/xout/scan-only.searchable.pdf" ] || { echo "SMOKE FAIL: no searchable copy"; exit 1; }
  pdftotext "$T/xout/scan-only.searchable.pdf" - 2>/dev/null | grep -qi zirconium \
    || { echo "SMOKE FAIL: searchable copy has NO extractable text layer"; exit 1; }
  sha_after=$(sha256sum "$T/docs/scan-only.pdf" | cut -d' ' -f1)
  [ "$sha_before" = "$sha_after" ] || { echo "SMOKE FAIL: export MODIFIED the original file"; exit 1; }
  run export --folders docs --out "$T/xout" --json > "$T/x2.json" 2>&1
  grep -q '"skipped_existing": 1' "$T/x2.json" \
    || { echo "SMOKE FAIL: rerun re-exported an existing copy"; cat "$T/x2.json"; exit 1; }
  echo "   searchable copy carries real text; original byte-identical; rerun keeps it"

  timeout 120 "$AI" --appimage-extract-and-run mcp-serve --transport http \
      --host 127.0.0.1 --port 8797 --token "$MTOK" > "$T/mcp3.log" 2>&1 &
  M3=$!
  for _ in $(seq 1 60); do grep -q "Uvicorn running" "$T/mcp3.log" && break; sleep 0.5; done
  grep -q "Uvicorn running" "$T/mcp3.log" || { echo "SMOKE FAIL: mcp (dl gate) never came up"; cat "$T/mcp3.log"; exit 1; }
  DTOK="$DTOK" TTOK="$TTOK" MTOK="$MTOK" "$PY" - >"$T/docid" <<'PYX'
import json, os, sys, urllib.error, urllib.request
B, DTOK, TTOK, MTOK = ("http://127.0.0.1:8797",
                       os.environ["DTOK"], os.environ["TTOK"], os.environ["MTOK"])
fail = []

def get(path, tok):
    url = B + path + "?" + ("tok" + "en=") + tok
    try:
        r = urllib.request.urlopen(url)
        return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()

def rpc(tok, sid, payload):
    url = B + "/mcp?" + ("tok" + "en=") + tok
    h = {"Content-Type": "application/json",
         "Accept": "application/json, text/event-stream"}
    if sid:
        h["Mcp-Session-Id"] = sid
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=h)
    r = urllib.request.urlopen(req)
    sid = r.headers.get("mcp-session-id") or sid
    body = r.read().decode()
    datas = [l[5:].strip() for l in body.splitlines() if l.startswith("data:")]
    txt = datas[-1] if datas else body
    return sid, (json.loads(txt) if txt.strip() else None)

def init(tok):
    sid, _ = rpc(tok, None, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "smoke", "version": "0"}}})
    rpc(tok, sid, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    return sid

def call(tok, sid, name, args):
    _, res = rpc(tok, sid, {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                            "params": {"name": name, "arguments": args}})
    r = res["result"]
    return r["content"][0]["text"] if r.get("content") else ""

try:
    # find Pc's doc id as the files-granted profile sees it
    sid = init(DTOK)
    hits = json.loads(call(DTOK, sid, "search", {"query": "DLPROBE", "mode": "lexical"}))
    if not hits: fail.append("files-granted profile cannot see its granted doc")
    doc_id = hits[0]["doc_id"] if hits else 1
    sys.stdout.write(str(doc_id))
    # folder master switch beats a profile files-grant: both profiles 403
    for who, tok in (("text-only", TTOK), ("files-granted", DTOK)):
        code, body = get("/file/%d" % doc_id, tok)
        if code != 403: fail.append(f"folder-blocked {who} profile /file got {code}, want 403")
    # admin always downloads their own library, even master-blocked folders
    code, body = get("/file/%d" % doc_id, MTOK)
    if code != 200 or b"DLPROBE" not in body:
        fail.append(f"admin /file blocked-folder download got {code}")
    # unknown credential is a 401 on this route too (same gate as MCP)
    try:
        code, _ = get("/file/%d" % doc_id, "definitely-not-a-token")
        if code != 401: fail.append(f"bogus token /file got {code}, want 401")
    except urllib.error.HTTPError as e:
        if e.code != 401: fail.append(f"bogus token /file got {e.code}, want 401")
    # file_url tool honors the same policy (folder still blocked here)
    fu = json.loads(call(DTOK, sid, "file_url", {"doc_id": doc_id}))
    if "error" not in fu: fail.append("file_url gave a URL while the FOLDER blocks")
except Exception as e:
    fail.append(f"dl gate crashed: {e!r}")
sys.exit(("\n".join("DOWNLOAD GATE: " + f for f in fail)) if fail else 0)
PYX
  DLRC=$?
  [ $DLRC -eq 0 ] || { kill $M3 2>/dev/null; exit 1; }
  DOCID=$(cat "$T/docid")
  run folder-files Pc >/dev/null                # lift the master block
  DTOK="$DTOK" TTOK="$TTOK" DOCID="$DOCID" "$PY" - <<'PYX'
import os, sys, urllib.request
B, DTOK, TTOK = ("http://127.0.0.1:8797", os.environ["DTOK"], os.environ["TTOK"])
fail = []

def get(path, tok):
    url = B + path + "?" + ("tok" + "en=") + tok
    try:
        r = urllib.request.urlopen(url)
        return r.status, r.read()
    except Exception as e:
        code = getattr(e, "code", None)
        if code: return code, e.read()
        raise

DOCID = os.environ["DOCID"]
code, body = get("/file/" + DOCID, DTOK)
if code != 200 or b"DLPROBE" not in body: fail.append(f"files-granted download got {code}, want 200 bytes")
code, body = get("/file/" + DOCID, TTOK)
if code != 403 or b"text-only" not in body: fail.append(f"text-only profile got {code}, want 403 text-only")
sys.exit(("\n".join("DOWNLOAD GATE: " + f for f in fail)) if fail else 0)
PYX
  DLRC=$?
  kill $M3 2>/dev/null; wait $M3 2>/dev/null || true
  [ $DLRC -eq 0 ] || exit 1
  echo "   download hierarchy enforced over the wire (folder master > profile grant > admin override)"

  run status
  # double-click path: bare `ui` must come up, and a second bare `ui` must
  # slide to a free port instead of dying (v0.2.0 regression class).
  # Port-agnostic so it also runs on machines where 8765 is already taken.
  echo "-- ui (default port):"
  wait_port() {  # $1=log  $2=max seconds; echoes first 127.0.0.1:PORT seen
    local p
    for _ in $(seq 1 "$2"); do
      p=$(grep -oE '127\.0\.0\.1:[0-9]+' "$1" | head -1 | cut -d: -f2 || true)
      [ -n "$p" ] && { echo "$p"; return 0; }
      sleep 1
    done
    return 1
  }
  timeout 40 "$AI" --appimage-extract-and-run ui > "$T/ui1.log" 2>&1 &   # no --no-browser: true double-click path
  U1=$!
  P1=$(wait_port "$T/ui1.log" 30) || { echo "SMOKE FAIL: first UI never came up"; cat "$T/ui1.log"; exit 1; }
  curl -sf -o /dev/null "http://127.0.0.1:$P1/" && echo "   first UI answers on $P1"
  timeout 40 "$AI" --appimage-extract-and-run ui --no-browser > "$T/ui2.log" 2>&1 &
  U2=$!
  P2=$(wait_port "$T/ui2.log" 30) || { echo "SMOKE FAIL: second UI never came up"; cat "$T/ui2.log"; exit 1; }
  grep -q "busy" "$T/ui2.log" || { echo "SMOKE FAIL: second UI did not report sliding"; exit 1; }
  [ "$P2" != "$P1" ] || { echo "SMOKE FAIL: second UI reused port $P1"; exit 1; }
  curl -sf -o /dev/null "http://127.0.0.1:$P2/" && echo "   second UI slid to $P2 and answers"
  kill $U1 $U2 2>/dev/null
  wait $U1 $U2 2>/dev/null || true
  echo "== SMOKE OK"
  rm -rf "$T"
fi
