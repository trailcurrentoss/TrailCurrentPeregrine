#!/usr/bin/env bash
# train.sh — Launch wake word training inside WSL2.
#
# Usage (from PowerShell / CMD):
#   wsl -- bash train.sh                       # full pipeline
#   wsl -- bash train.sh --step check-env      # single step
#   wsl -- bash train.sh --from augment        # resume
#   wsl -- bash train.sh --list-steps          # show steps
#
# Or from within WSL2:
#   bash train.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "========================================"
echo "  openWakeWord Trainer"
echo "========================================"
echo ""
echo "Project root: $SCRIPT_DIR"
echo ""

# ── Check we're on Linux (WSL2) ──
if [[ "$(uname -s)" != "Linux" ]]; then
    echo "ERROR: This script must run inside WSL2 (Linux)."
    echo "       piper-phonemize requires Linux."
    echo ""
    echo "Try:  wsl -- bash train.sh"
    exit 1
fi

# ── Create / activate a virtual environment for training ──
# Use WSL2 native filesystem for the venv (symlinks don't work on /mnt/c/)
VENV_DIR="$HOME/.oww-trainer-venv"

if [ ! -d "$VENV_DIR" ]; then
    echo "Creating training virtualenv at $VENV_DIR ..."
    python3 -m venv "$VENV_DIR"
fi

source "$VENV_DIR/bin/activate"
echo "Using Python: $(which python3)  ($(python3 --version))"

# ── Install training dependencies ──
echo ""
echo "Installing training dependencies ..."
pip install --upgrade pip -q

# Pin setuptools < 82 to keep pkg_resources available
pip install 'setuptools<82' -q

pip install -r "$SCRIPT_DIR/requirements.txt" -q

# ── Run the training script ──
# All CLI flags (--step, --from, --verify-only, --list-steps, --config) are
# forwarded transparently via "$@".
echo ""
echo "Starting training pipeline ..."
cd "$SCRIPT_DIR"
python3 train_wakeword.py "$@"

echo ""
echo "Done!  Deactivating training venv."
deactivate
