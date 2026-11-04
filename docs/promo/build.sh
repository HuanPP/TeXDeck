#!/bin/sh
# Rebuild docs/promo.webp (and build/promo.mp4) from promo.html.
#
#   docs/promo/build.sh DECKS_DIR
#
# DECKS_DIR: compiled templates, e.g. from
#   cd server && ../.venv/bin/python -m pytest -m slow --basetemp=/tmp/texdeck-decks
# Needs uv, ffmpeg with libwebp, and the repo .venv (for pymupdf).
# Set CHROMIUM=/usr/bin/chromium to skip Playwright's browser download.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
B="$HERE/build"

"$REPO/.venv/bin/python" -I "$HERE/render_assets.py" "$1"

if [ ! -x "$B/venv/bin/python" ]; then
  uv venv -q "$B/venv" --python 3.12
  uv pip install -q playwright --python "$B/venv/bin/python"
  [ -n "$CHROMIUM" ] || "$B/venv/bin/python" -m playwright install chromium
fi

rm -rf "$B/frames"
"$B/venv/bin/python" -I "$HERE/record.py" 20

ffmpeg -loglevel error -y -framerate 20 -i "$B/frames/f%04d.png" \
  -c:v libx264 -preset slow -crf 17 -pix_fmt yuv420p -movflags +faststart "$B/promo.mp4"
# light denoise strips the film grain, which otherwise doubles the file size
ffmpeg -loglevel error -y -framerate 20 -i "$B/frames/f%04d.png" -vf "hqdn3d=4:4:8:8" \
  -c:v libwebp -quality 70 -compression_level 6 -loop 0 "$REPO/docs/promo.webp"
ls -la "$REPO/docs/promo.webp" "$B/promo.mp4"
