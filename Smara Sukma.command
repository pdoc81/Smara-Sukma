#!/bin/zsh
# Smara Sukma — double-click to recover deleted photos & videos.
cd "$(dirname "$0")"
PY="$(command -v python3)"
echo "Your Mac password is needed to read the drive directly (read-only)."
sudo "$PY" smara_sukma.py "$@"
echo
read "?Done. Press Enter to close this window."
