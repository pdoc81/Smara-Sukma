#!/bin/sh
# Smara Sukma — Linux launcher for recovering deleted photos & videos.
cd "$(dirname "$0")"
PY="$(command -v python3)"
echo "Root access is needed to read the drive directly (read-only)."
sudo "$PY" smara_sukma.py "$@"
