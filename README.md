# Voice Mode for Orca

Talk to your Claude agents in Orca and hear their replies. Speech in and out runs on your Mac, with no extra
account and no per-use cost. Claude Code, signed in as you normally are, is still the brain.

You get a sidebar panel in Orca to control it, a talk key for dictation, and spoken replies from whichever
Claude session you are working in.

## What you need

- A Mac with Apple Silicon (M1 or newer). The speech models run on Apple's MLX, so Intel Macs, Windows and
  Linux are not supported.
- [Orca](https://github.com/stablyai/orca) with plugin support (1.4.246 or newer) and Claude Code.
- [Homebrew](https://brew.sh). Setup uses it to install `uv` and `ffmpeg` if they are missing.
- About 6 GB of free space (setup uses about 4 GB, and the optional Whisper model adds about 1.5 GB) and an internet connection for the first setup.

## Install

1. In a terminal, run `orca plugin install skyshab/orca-voice-mode`. It stops and lists the access the plugin
   asks for: it can run code as you, and read your workspaces and agent status (used to follow the workspace you
   are working in). If that is fine, re-run the command with the `--approve` value it prints.
2. Open the **Voice Mode** panel in Orca's right sidebar and press **Run setup**. It installs the tools, builds a
   Python environment and downloads the speech models. This takes a few minutes.
3. Press **Start**. The first start takes about half a minute while the models load.

## What setup installs

Setup (the panel's **Run setup** button, which runs `scripts/setup.sh`) is safe to run again. It asks before it
installs anything with Homebrew.

| What | Used for | Size | Where it goes |
|---|---|---|---|
| `uv` (Homebrew) | Builds and runs the Python helper. | small | Homebrew |
| `ffmpeg` (Homebrew) | Its `ffplay` plays the spoken replies. | about 0.1 GB, plus its dependencies | Homebrew |
| Python 3.12 and packages | The helper, plus the MLX speech libraries. `uv` fetches Python itself. | about 1.4 GB | `helper/.venv` in the plugin folder |
| Kokoro (`Kokoro-82M`) | The voice that reads replies aloud. | about 0.3 GB | Hugging Face cache, `~/.cache/huggingface` |
| A small spaCy English model | Kokoro uses it to split text into words. It downloads from GitHub on first use. | about 15 MB | `helper/.venv` |
| Parakeet (`parakeet-tdt-0.6b-v2`) | Local dictation (the default). | about 2.3 GB | Hugging Face cache |
| Whisper (`large-v3-turbo`) | Optional local dictation, better with unusual words. It downloads the first time you choose it. | about 1.5 GB | Hugging Face cache |

Setup also links the `voice` and `voice-hook` commands into `~/.local/bin`. Add that folder to your `PATH` if you
want to use `voice` in a terminal. Setup does not touch Karabiner-Elements or your Claude Code settings.

## If setup fails

The panel shows the last lines of the setup log, and the full log is in `~/.voice-bridge/setup.log`. The usual
causes are no internet connection, no Homebrew, or too little disk space. You can also do each step by hand,
from the plugin folder:

1. Install the tools: `brew install uv ffmpeg`.
2. Build the Python environment: `cd helper && uv sync`.
3. Download the models: `uv run python src/fetch_models.py` (still in `helper`).
4. Link the commands: `ln -sfn "$PWD/../bin/voice" ~/.local/bin/voice` and the same for `voice-hook`.
5. Start the helper: `voice start`, then run `voice doctor`.

If a model download stops halfway, run step 3 again. It picks up where it left off. To free the space later,
delete `helper/.venv` and the model folders in `~/.cache/huggingface/hub` (their names start with `models--mlx-community`).

## Permissions macOS asks for

macOS grants these to the app that starts the helper, which is Orca when you start it from the panel.

| Permission | Why |
|---|---|
| Microphone | Records your dictation. |
| Input Monitoring | Hears the talk key while another app has focus. |
| Accessibility | Pastes your dictation into the focused window, and presses Enter if you turn that on. |

Press **Check permissions** in the panel (or run `voice doctor`) to see which one is missing. After you allow
one in System Settings, restart the helper.

## Use

- **Talk key.** Hold it to talk and release to send, or tap it twice to start and let it stop after a few
  seconds of silence. Tap once to stop sooner. The default is F18, which most keyboards do not have. Pick Right
  Command, Right Option or Right Control in the panel instead, or map a key or remote button to F18 with
  Karabiner-Elements.
- **Dictation source.** Parakeet (fast) or Whisper (larger, better with unusual words) run locally. If you use
  Wispr Flow, choose it and the helper only speaks replies.
- **Spoken replies.** The voice reads the reply of the session you are working in. Talking, or pressing the
  panel's Shut up button, stops it.
- **Agent replies written for the ear.** The "short spoken-style replies" switch adds a small hook to Claude
  Code. While voice is on, agents write short plain sentences. While you mute, they go back to normal.
- **Pause other audio.** Dictation pauses music or video that is playing, as Wispr Flow can. It does not
  resume it.
- **Hands-free conversation.** After each spoken reply the microphone opens again by itself.

Everything the panel does is also a `voice` command: `voice --help` lists them. Settings, logs and your recent
recordings live in `~/.voice-bridge/`. To add names or jargon that Whisper should expect, put them in
`~/.voice-bridge/vocab.txt`.

## Troubleshooting

- Run `voice doctor` first.
- Dictation records nothing: check the Mac's input volume and mute state, and the Microphone permission.
- The voice comes from the wrong speakers: it follows the Mac's current output.
- Wispr Flow stopped responding after a crash: run `voice mode wispr`.

## Uninstall

Remove the plugin in Orca, delete this folder and `~/.voice-bridge`, and delete the two links
`~/.local/bin/voice` and `~/.local/bin/voice-hook`. Turn off the hook switch in the panel first, or remove the
`voice-hook` entry from `~/.claude/settings.json`.

## For developers

`src/` is the Orca panel and its worker (TypeScript, no build step). `helper/` is the Python helper that does the
listening and speaking. The two talk over a local socket (`~/.voice-bridge/control.sock`). `orca plugin dev watch .`
loads the plugin for development, and `orca plugin test .` runs the worker tests.

## License

MIT. See `LICENSE`. The speech models and libraries that setup downloads have their own licenses.
