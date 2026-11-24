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
