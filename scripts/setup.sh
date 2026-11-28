#!/bin/bash
# One-time setup for Voice Mode. Safe to run again. It tells you plainly what it did and what it could not do.
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HELPER="$ROOT/helper"
# Outside the plugin folder: Orca rejects symlinks in installed plugin content, and a venv is full of them.
export UV_PROJECT_ENVIRONMENT="$HOME/.voice-bridge/venv"
mkdir -p "$HOME/.voice-bridge"
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
problems=0

say() { printf '\n== %s\n' "$1"; }
ask_yes() { [ "${VOICE_SETUP_YES:-}" = "1" ] && return 0; read -r -p "$1 [y/N] " a; [ "$a" = "y" ] || [ "$a" = "Y" ]; }

say "Checking this Mac"
if [ "$(uname -s)" != "Darwin" ]; then echo "Voice Mode only runs on macOS."; exit 1; fi
if [ "$(uname -m)" != "arm64" ]; then
  echo "Voice Mode needs an Apple Silicon Mac (M1 or newer): its speech models run on Apple's MLX."
  exit 1
fi
echo "Apple Silicon Mac: ok"

say "Orca"
if command -v orca >/dev/null 2>&1 || [ -x /Applications/Orca.app/Contents/Resources/bin/orca ]; then echo "Orca command line: found"
else echo "Orca was not found. Install Orca, open it once, then run this again."; problems=$((problems+1)); fi

install_with_brew() {  # $1 = command, $2 = brew formula
  if command -v "$1" >/dev/null 2>&1; then echo "$1: found"; return; fi
  if command -v brew >/dev/null 2>&1; then
    if ask_yes "$1 is missing. Install it with Homebrew (brew install $2)?"; then brew install "$2" || problems=$((problems+1)); else problems=$((problems+1)); fi
  else
    echo "$1 is missing, and Homebrew is not installed. Install Homebrew from https://brew.sh, then run this again."
    problems=$((problems+1))
  fi
}
say "Tools"
install_with_brew uv uv
install_with_brew ffplay ffmpeg

say "Python environment and speech models"
if command -v uv >/dev/null 2>&1; then
  (cd "$HELPER" && uv sync) || { echo "Could not build the Python environment."; problems=$((problems+1)); }
  echo "Downloading the speech models (a few hundred megabytes; the larger Whisper model downloads on first use)..."
  (cd "$HELPER" && uv run python src/fetch_models.py) || { echo "Model download failed. Starting the helper will try again."; problems=$((problems+1)); }
fi

say "The voice command"
mkdir -p "$HOME/.local/bin"
for f in voice voice-hook; do ln -sfn "$ROOT/bin/$f" "$HOME/.local/bin/$f" && echo "linked ~/.local/bin/$f"; done
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "Add ~/.local/bin to your PATH to use the 'voice' command in a terminal.";; esac

say "Done"
if [ "$problems" -eq 0 ]; then
  echo "Setup finished. Start the helper from the Voice Mode panel in Orca, or run: voice start"
  echo "On first use macOS will ask for Microphone access. Run 'voice doctor' afterwards to check the other permissions."
else
  echo "Setup finished with $problems problem(s) above. Fix them and run this again."
  exit 1
fi
