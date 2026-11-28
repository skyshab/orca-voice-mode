"""Voice helper for Orca. Starts once, runs in the background, works with any Claude session.

It follows the Claude session you used most recently (the last one that started working on a
prompt) and reads that session's replies aloud. In local mode, holding the talk key transcribes
what you say and pastes it into whatever window has focus, like Wispr Flow does (it does not press
Enter unless you turn that on). In wispr mode, Wispr Flow does the typing and this helper only
does the speaking.

Control it with the `voice` command (see src/voice_ctl.py), not by typing into this process:
  voice start | voice mode local | voice mode wispr | voice mute | voice unmute
  voice stt parakeet | voice stt whisper | voice limit N | voice enter on|off | voice stop | voice status | voice quit
"""
import argparse
import json
import math
import os
import re
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import sounddevice as sd
import soundfile as sf

import speak
from orca_cli import orca

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # the helper folder
DATA = os.path.expanduser("~/.voice-bridge")  # settings, logs, recordings and the control socket
os.makedirs(DATA, exist_ok=True)
LOG = os.path.join(DATA, "voice-log.jsonl")
# A helper started from an app (Orca) does not get the shell's PATH, so add where Homebrew keeps ffplay and uv.
os.environ["PATH"] = os.pathsep.join(["/opt/homebrew/bin", "/usr/local/bin", os.path.expanduser("~/.local/bin"), os.environ.get("PATH", "")])
SOCK = os.path.join(DATA, "control.sock")
SR = 16000
# Newer Karabiner-Elements keeps its command line tool inside the app; older ones put it under /Library.
KARABINER = next((p for p in ("/Applications/Karabiner-Elements.app/Contents/Helpers/karabiner_cli",
                              "/Library/Application Support/org.pqrs/Karabiner-Elements/bin/karabiner_cli")
                  if os.path.exists(p)), "")
SETTINGS = os.path.join(DATA, "settings.json")

state = {"mode": "wispr", "stt": "parakeet", "muted": False, "recording": False, "held": False, "live": False, "current": None, "enter": False, "cue": "Tink", "silence": 2.0, "conversation": False, "mic": "default", "speed": 1.15, "speaker": "af_bella", "pause": True, "key": "f18"}
names = {}  # session id -> "workspace: title", for status
transcribers = {}


def log(**kw):
    kw["t"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG, "a") as f:
        f.write(json.dumps(kw) + "\n")


SAVED_KEYS = ("mode", "stt", "muted", "enter", "cue", "silence", "mic", "speed", "speaker", "pause", "key", "conversation")


def load_settings():
    """Restore every saved setting, including conversation mode, from the last run."""
    try:
        d = json.load(open(SETTINGS))
    except (OSError, ValueError):
        return
    for k in SAVED_KEYS:
        if k in d:
            state[k] = d[k]
    speak.SPEED = state["speed"]
    speak.VOICE = state["speaker"]
    if isinstance(d.get("limit"), int):
        speak.MAX_WORDS = d["limit"]


def save_settings():
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    with open(SETTINGS, "w") as f:
        json.dump({**{k: state[k] for k in SAVED_KEYS}, "limit": speak.MAX_WORDS}, f)


def sync_karabiner(local):
    """Tell Karabiner whether local mode is on. Its rule turns right Option into F18 only then,
    so the same key or remote button reaches Wispr Flow in wispr mode and this helper in local mode."""
    if not KARABINER:
        return
    subprocess.run([KARABINER, "--set-variables", json.dumps({"voice_local": 1 if local else 0})],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


closing = threading.Event()


def keep_karabiner_in_sync():
    """Karabiner forgets its variables whenever its config reloads or it restarts, which would turn the
    remote and right Option off without a word. Say it again every few seconds."""
    while not closing.wait(10):
        sync_karabiner(state["mode"] == "local")


MAX_TURNS = 10  # a hands-free conversation ends by itself after this many replies
conv = {"active": False, "turns": 0, "listen": None}
SOUNDS = "/System/Library/Sounds"
tickd = {}


def build_tick(name, out="start-sound.wav"):
    """Load a macOS system sound, mix it to identical left and right, and put a quarter second of silence
    in front. Bluetooth headphones can be slow to wake their audio path, and a sound that arrives
    mid-wake can reach only one ear. Returns the name actually used."""
    files = {f[:-5].lower(): f[:-5] for f in os.listdir(SOUNDS) if f.endswith(".aiff")}
    if name.lower() not in files:
        raise ValueError("unknown sound. Options: " + ", ".join(sorted(files.values())))
    name = files[name.lower()]
    d, sr = sf.read(f"{SOUNDS}/{name}.aiff", dtype="float32")
    mono = d.reshape(len(d), -1).mean(axis=1)
    loud = np.nonzero(np.abs(mono) > 0.01 * np.abs(mono).max())[0]
    mono = mono[: loud[-1] + 1][: int(sr * 0.8)]  # drop trailing silence, cap at 0.8s
    wave = np.concatenate([np.zeros(int(sr * 0.25)), mono])
    stereo = np.stack([wave, wave], axis=1).astype("float32")
    path = os.path.expanduser("~/.voice-bridge/" + out)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    sf.write(path, stereo, sr)
    if out == "start-sound.wav":
        tickd.update(path=path, secs=len(wave) / sr)
    return name


def end_cue():
    """The sound for ending a conversation by voice. It gets the same lead-in silence as the start sound,
    or a Bluetooth headset swallows it."""
    path = os.path.expanduser("~/.voice-bridge/end-sound.wav")
    if not os.path.exists(path):
        build_tick("Pop", "end-sound.wav")
    subprocess.Popen(["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def tick():
    """Play the start sound in a separate afplay process. Playing it through the audio library inside this
    process once hung forever in Pa_CloseStream (double-tap restarted a stream mid-play on the AirPods)."""
    old = tickd.get("proc")
    if old and old.poll() is None:
        old.terminate()
    tickd["proc"] = subprocess.Popen(["afplay", tickd["path"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def kdbg(msg):
    """Key diagnostics with milliseconds, to compare against what macOS reports."""
    print(time.strftime("%H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d} KEY {msg}", flush=True)


def say(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def cue(name):
    """Short system sound so you know when listening starts and stops."""
    subprocess.Popen(["afplay", f"/System/Library/Sounds/{name}.aiff"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --- speech to text -------------------------------------------------------------------------
# MLX streams belong to the thread that created them, so every model is loaded and run on this
# one worker thread. Calling it from any other thread breaks.
stt_pool = ThreadPoolExecutor(max_workers=1)


def _load(name):
    if name == "parakeet":
        from parakeet_mlx import from_pretrained
        m = from_pretrained("mlx-community/parakeet-tdt-0.6b-v2")
        return lambda path: m.transcribe(path).text.strip()
    import mlx_whisper
    # Words for Whisper to expect (names, jargon): one list in the user's data folder, else the shipped one.
    vocab = next((p for p in (os.path.join(DATA, "vocab.txt"), os.path.join(HERE, "vocab.txt")) if os.path.exists(p)), "")
    hint = open(vocab).read().strip() if os.path.exists(vocab) else None
    return lambda path: mlx_whisper.transcribe(
        path, path_or_hf_repo="mlx-community/whisper-large-v3-turbo", language="en", initial_prompt=hint)["text"].strip()


def _run(name, path):
    if name not in transcribers:
        transcribers[name] = _load(name)
    if path is None:  # warm-up: the first call compiles kernels, so run one on a second of silence
        with tempfile.TemporaryDirectory() as d:
            quiet = os.path.join(d, "quiet.wav")
            sf.write(quiet, np.zeros(SR, dtype="float32"), SR)
            transcribers[name](quiet)
        return None
    return transcribers[name](path)


def transcribe(name, path):
    return stt_pool.submit(_run, name, path).result()


def preload(name):
    stt_pool.submit(_run, name, None).result()


# --- speaking -------------------------------------------------------------------------------
def continue_conversation():
    """Hands-free mode: after a reply has been spoken, reopen the microphone so no tap is needed."""
    if not (state["conversation"] and conv["active"] and conv["listen"]):
        return
    if speak.last_cancelled() or state["muted"] or state["mode"] != "local":
        conv["active"] = False  # you cut the reply off or changed something: you have taken over
        return
    conv["turns"] += 1
    if conv["turns"] > MAX_TURNS:
        conv["active"] = False
        say(f"conversation ended after {MAX_TURNS} turns")
        return
    time.sleep(0.6)  # let the speakers' tail die away so it is not recorded
    if conv["active"] and conv["listen"]():
        say("listening again")


class Talker(threading.Thread):
    """Speaks replies one at a time, off the main thread."""
    def __init__(self):
        super().__init__(daemon=True)
        import queue
        self.q = queue.Queue()

    def say(self, text):
        self.q.put(text)

    def clear(self):
        while not self.q.empty():
            try:
                self.q.get_nowait()
            except Exception:
                break

    def run(self):
        while True:
            text = self.q.get()
            if state["muted"]:
                continue
            try:
                speak.speak(text, on_first_sound=lambda t: say(f"first sound {t:.1f}s after the reply was handed over"))
            except Exception as e:  # keep the loop alive if audio hiccups
                say(f"speech error: {e}")
            else:
                continue_conversation()


# --- following the Claude sessions ----------------------------------------------------------
def claude_sessions():
    return [a for a in orca("agent", "list")["agents"] if a.get("agent") == "claude"]


def full_reply(sid, fallback):
    """The session list only carries a short preview of the last reply, so read the whole message."""
    try:
        entries = orca("agent", "read", sid, "--limit", "10")["entries"]
        texts = [e["text"] for e in entries if e.get("kind") == "message" and e.get("role") == "assistant" and e.get("text")]
        return texts[-1] if texts else fallback
    except Exception as e:
        say(f"could not read full reply, using preview: {e}")
        return fallback


TERMINAL_SETTLE_SECS = 1.5  # how long a terminal session's latest message must stay unchanged before it is read


def cur_is_terminal(sessions, sid):
    return any(a["orcaSessionId"] == sid and a.get("kind") == "terminal" for a in sessions)


def stop_talking(talker):
    talker.clear()
    speak.cancel()


def follow(talker):
    """Track every Claude session. The one that most recently started working is the one we follow."""
    last_state, last_msg, t_start = {}, {}, {}
    pending = {}  # terminal session id -> (new message, when it first appeared)
    started = False
    while True:
        try:
            sessions = claude_sessions()
        except Exception as e:
            say(f"could not list sessions: {e}")
            time.sleep(2)
            continue
        if not started and sessions:
            # Start with the most recently active session, and do not read out old replies.
            state["current"] = max(sessions, key=lambda a: a.get("updatedAt", 0))["orcaSessionId"]
            started = True
        for a in sessions:
            sid, st, msg = a["orcaSessionId"], a["state"], a.get("lastAssistantMessage")
            names[sid] = f"{a.get('workspace')}: {a.get('title')}"
            if sid not in last_state:  # first sighting: remember it, do not announce it
                last_state[sid], last_msg[sid] = st, msg
                if started and state.get("focus") and (a.get("worktreeId") or "").lower() == state["focus"]:
                    # The focused workspace had no session when focus moved there; this is the one that just opened.
                    state["current"], state["focus"] = sid, None
                    say(f"now following: {names[sid]}")
                if st == "working" and started:
                    state["current"], t_start[sid] = sid, time.monotonic()
                    stop_talking(talker)
                continue
            if st == "working" and last_state[sid] != "working":
                if state["current"] != sid:
                    say(f"now following: {names[sid]}")
                state["current"], t_start[sid] = sid, time.monotonic()
                stop_talking(talker)  # a new prompt went out, so whatever was being read is stale
            if st == "done" and msg and msg != last_msg[sid]:
                last_msg[sid] = msg
                # A session that was already mid-turn when the helper started never "starts working" in
                # our eyes. If it finishes while the followed session is idle, move over to it.
                cur = state["current"]
                if sid != cur and last_state[sid] == "working" and last_state.get(cur) != "working":
                    say(f"now following: {names[sid]}")
                    state["current"] = sid
                if sid == state["current"]:
                    msg = full_reply(sid, msg)
                    took = time.monotonic() - t_start[sid] if sid in t_start else None
                    say(f"reply from {names[sid]}" + (f" ({took:.1f}s)" if took else "") + f": {msg[:300]}")
                    log(event="reply", mode=state["mode"], session=names[sid], seconds=took, words=len(msg.split()))
                    talker.say(msg)
            if a.get("kind") == "terminal" and st == "working" and msg and msg != last_msg[sid]:
                # Orca often leaves a terminal session on "working" after its turn ends, so a new message is
                # the only sign of a reply. Wait until the message stops changing, then read it.
                if pending.get(sid, (None,))[0] != msg:
                    pending[sid] = (msg, time.monotonic())
                elif time.monotonic() - pending[sid][1] >= TERMINAL_SETTLE_SECS:
                    last_msg[sid] = msg
                    del pending[sid]
                    cur = state["current"]
                    if sid != cur and (last_state.get(cur) != "working" or cur_is_terminal(sessions, cur)):
                        say(f"now following: {names[sid]}")
                        state["current"] = cur = sid
                    if sid == cur:
                        msg = full_reply(sid, msg)
                        say(f"reply from {names[sid]} (terminal, still working): {msg[:300]}")
                        log(event="reply", mode=state["mode"], session=names[sid], seconds=None, words=len(msg.split()))
                        talker.say(msg)
            else:
                pending.pop(sid, None)
            last_state[sid] = st
        time.sleep(0.4)


def paste(text, press_enter):
    """Type text into whatever window has focus, the way Wispr Flow does: copy, paste, restore the clipboard."""
    from pynput import keyboard
    kb = keyboard.Controller()
    old = subprocess.run(["pbpaste"], capture_output=True).stdout
    subprocess.run(["pbcopy"], input=text.encode())
    time.sleep(0.05)
    with kb.pressed(keyboard.Key.cmd):
        kb.tap("v")
    time.sleep(0.25)  # let the app read the clipboard before it is restored
    subprocess.run(["pbcopy"], input=old)
    if press_enter:
        kb.tap(keyboard.Key.enter)


# --- local mode: hold a key, speak, release ---------------------------------------------------
END_PHRASES = {"end conversation", "end the conversation", "that is all", "thats all", "thats it", "we are done", "were done", "stop listening", "goodbye"}


def handle_audio(frames, voiced_secs=1.0):
    audio = np.concatenate(frames).reshape(-1)
    clips = os.path.expanduser("~/.voice-bridge/clips")  # keep the last few recordings so odd results can be inspected
    os.makedirs(clips, exist_ok=True)
    sf.write(os.path.join(clips, time.strftime("%H%M%S") + ".wav"), audio, SR)
    for old in sorted(os.listdir(clips))[:-12]:
        os.remove(os.path.join(clips, old))
    say(f"clip {len(audio) / SR:.1f}s, speech {voiced_secs:.1f}s, peak {float(np.abs(audio).max()):.3f}")
    if len(audio) < SR * 0.4:
        say("too short, ignored")
        return
    if voiced_secs < 0.2:  # room noise only: the speech model would invent words like "Mm-hmm"
        say("no speech heard, nothing sent")
        conv["active"] = False
        return
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "clip.wav")
        sf.write(path, audio, SR)
        t = time.perf_counter()
        text = transcribe(state["stt"], path)
        took = time.perf_counter() - t
    if not text:
        say("heard nothing")
        conv["active"] = False
        return
    say(f"you [{state['stt']}, {took:.2f}s]: {text}")
    log(event="heard", mode="local", stt=state["stt"], seconds=took, audio_seconds=len(audio) / SR, text=text)
    if re.sub(r"[^a-z ]", "", text.lower()).strip() in END_PHRASES:
        # Say one of these on its own to end this exchange. Conversation mode stays on for the next one.
        conv["active"] = False
        end_cue()
        say("conversation ended by voice, Conversation mode is still on")
        return
    try:
        paste(text, state["enter"])
    except Exception as e:
        cue("Basso")
        say(f"could not type into the focused window: {e}")


KEYCODES = {"f13": 105, "f14": 107, "f15": 113, "f16": 106, "f17": 64, "f18": 79, "f19": 80, "f20": 90}
MAX_HOLD = 120      # seconds, a backstop for a held key
MAX_LATCHED = 300   # seconds, a backstop for a double-tap recording
DOUBLE_TAP = 0.5    # seconds from a tap's release to the next press that counts as a double tap
audio_idle = threading.Event()  # set while no microphone stream is open
audio_idle.set()
rec = {"frames": [], "stream": None, "t0": 0.0, "latched": False, "hist": [], "ema": 0.0, "acc": 0.0, "last_voiced": 0.0, "voiced_secs": 0.0}
gate = threading.Lock()


def on_audio(d):
    """Microphone callback: keep the audio and track when speech was last heard (for auto-stop on silence).

    The microphone delivers tiny chunks (about 1 ms), so the room's quiet level is kept in 50 ms buckets
    covering the last minute. Judging "quiet" from only the last fraction of a second made steady speech
    look like silence, which cut people off mid-sentence."""
    rec["frames"].append(d.copy())
    dt = len(d) / SR
    rms = float(np.sqrt((d ** 2).mean()))
    # Smooth over about 150 ms so a single noisy slice of room sound does not count as speech.
    rec["ema"] += (1 - math.exp(-dt / 0.15)) * (rms - rec["ema"])
    rec["acc"] += dt
    hist = rec["hist"]
    if rec["acc"] >= 0.05:
        hist.append(rec["ema"])
        rec["acc"] = 0.0
        if len(hist) > 1200:
            del hist[:200]
    floor = float(np.percentile(hist, 5)) if len(hist) >= 20 else 0.0  # the quietest moments: the room, not the speaker
    if rec["ema"] > max(0.003, 4 * floor):
        rec["last_voiced"] = time.monotonic()
        rec["voiced_secs"] += dt


OWN_AUDIO = ("ffplay", "afplay", "python", "voiced", "say")  # our own sounds do not count as "other audio"


def other_audio_playing():
    """True when some app other than this helper is producing sound. macOS lists a "audio-out" assertion,
    with the process that created it, for as long as an app is playing."""
    try:
        out = subprocess.run(["pmset", "-g", "assertions"], capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return False
    pid = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Created for PID:"):
            pid = line.split(":", 1)[1].strip(" .")
        elif line.startswith("Resources:") and "audio-out" in line and pid:
            name = subprocess.run(["ps", "-p", pid, "-o", "comm="], capture_output=True, text=True).stdout.strip().lower()
            if name and not any(o in name for o in OWN_AUDIO):
                return True
    return False


def pause_other_audio():
    """Send the Play/Pause media key if something else is playing. The key toggles, so it is only sent when
    sound is actually coming out. A player can keep reporting sound for a few seconds after it pauses, so
    a second press within that window is skipped rather than un-pausing the music."""
    if not state["pause"] or time.monotonic() - state.get("paused_t", -99) < 6 or not other_audio_playing():
        return
    from pynput import keyboard
    kb = keyboard.Controller()
    kb.tap(keyboard.Key.media_play_pause)
    state["paused_t"] = time.monotonic()
    say("paused other audio")
    time.sleep(0.25)  # a moment for the player to react before the start click


def pick_mic():
    """Index of the preferred microphone (by name), or None for the system default. Sound can go to the
    headphones while a different microphone listens, which keeps AirPods out of their low-quality call mode."""
    want = state["mic"].lower()
    if want != "default":
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0 and want in d["name"].lower():
                return i
    return None


def refresh_audio():
    """PortAudio lists the audio devices only once, so a headset connected after start-up would be ignored.
    Re-read the list while nothing is open."""
    sd._terminate()
    sd._initialize()


def finish(why=None, discard=False):
    """End the recording and send it for transcription. Safe to call from any thread, and more than once."""
    with gate:
        state["held"] = False
        if not state["live"]:
            return
        state["live"] = False
        state["recording"] = False
        rec["latched"] = False
        stream, frames, voiced = rec["stream"], rec["frames"], rec["voiced_secs"]
    if why:
        say(why)

    def work():
        try:
            stream.stop()  # can block while the AirPods switch modes, so never on the key listener's thread
            stream.close()
        finally:
            audio_idle.set()
        if not discard:
            handle_audio(frames, voiced)

    threading.Thread(target=work, daemon=True).start()


def run_hotkey(key_name):
    """Hold the key to talk, or double-tap it to start and tap once more to stop."""
    from pynput import keyboard
    key = getattr(keyboard.Key, key_name, None) or keyboard.KeyCode.from_char(key_name)
    code = KEYCODES.get(key_name)
    ui = {"down": False, "last_event": 0.0, "last_tap": 0.0, "gen": 0, "stop_press": False, "press_t": 0.0}

    def key_is_down():
        """Ask macOS whether the key is physically down, in case a release event was dropped."""
        if code is None:
            return True
        import Quartz
        return bool(Quartz.CGEventSourceKeyState(Quartz.kCGEventSourceStateCombinedSessionState, code))

    def begin(gen, latched):
        # AirPods swallow any sound that plays while the mic is opening or open, so the tick plays
        # in full first and recording starts right after it.
        speak.wait_idle()  # an interrupted reply must release the audio device before the click plays
        pause_other_audio()
        tick()
        time.sleep(tickd["secs"] + 0.5)  # let the sound finish before the mic opens (AirPods swallow it if the mic opens too soon)
        with gate:
            if ui["gen"] != gen:
                return  # a newer press took over (the second tap of a double tap)
            if not latched and (not state["held"] or not key_is_down()):
                state["held"] = False  # released during the tick: a tap, nothing to record
                state["recording"] = False
                return
            audio_idle.wait(2)
            refresh_audio()  # pick up whichever microphone is the default right now
            rec["frames"], rec["hist"], rec["last_voiced"], rec["voiced_secs"], rec["ema"], rec["acc"] = [], [], 0.0, 0.0, 0.0, 0.0
            rec["stream"] = sd.InputStream(samplerate=SR, channels=1, dtype="float32", device=pick_mic(), callback=lambda d, f, t, s: on_audio(d))
            rec["stream"].start()
            audio_idle.clear()
            rec["t0"] = time.monotonic()
            state["live"] = True

    def listen_now():
        """Open a double-tap style listening turn without a key press (hands-free mode)."""
        if state["recording"] or state["live"] or state["mode"] != "local":
            return False
        ui["gen"] += 1
        ui["press_t"] = time.monotonic()
        state["recording"] = True
        state["held"] = False
        rec["latched"] = True
        threading.Thread(target=begin, args=(ui["gen"], True), daemon=True).start()
        return True

    conv["listen"] = listen_now

    def on_press(k):
        if k != key:
            return
        now = time.monotonic()
        repeat = ui["down"] and now - ui["last_event"] < 0.6  # auto-repeat while the key is held
        ui["last_event"] = now
        if repeat:
            return
        ui["down"] = True
        kdbg(f"press (mode={state['mode']}, recording={state['recording']}, live={state['live']}, latched={rec['latched']})")
        if state["mode"] != "local":
            return
        if rec["latched"]:
            if state["live"]:  # any press ends a double-tap recording
                ui["stop_press"] = True
                threading.Thread(target=finish, daemon=True).start()
            return  # (still opening the mic: ignore)
        if now - ui["last_tap"] < DOUBLE_TAP:  # second tap: keep recording after the key is let go
            ui["last_tap"] = 0.0
            ui["gen"] += 1
            state["recording"] = True
            state["held"] = False
            rec["latched"] = True
            ui["press_t"] = now
            if state["conversation"]:
                conv["active"], conv["turns"] = True, 0
            speak.cancel()
            threading.Thread(target=begin, args=(ui["gen"], True), daemon=True).start()
            return
        conv["active"] = False  # a single press while idle or while it speaks: you have taken over
        if state["recording"]:
            return
        speak.cancel()  # talking over a reply stops it
        ui["gen"] += 1
        ui["press_t"] = now
        state["recording"] = True
        state["held"] = True
        threading.Thread(target=begin, args=(ui["gen"], False), daemon=True).start()

    def on_release(k):
        if k != key:
            return
        ui["down"] = False
        kdbg(f"release (held={state['held']}, live={state['live']}, latched={rec['latched']})")
        if ui["stop_press"]:
            ui["stop_press"] = False  # the release of the press that ended a double-tap recording
            return
        if rec["latched"]:
            return  # the release of the second tap: keep recording
        state["held"] = False  # begin() sees this if the release came during the tick
        if not state["live"] and state["recording"]:
            ui["last_tap"] = time.monotonic()  # let go during the tick: a tap, maybe the first of two
        threading.Thread(target=finish, daemon=True).start()  # never block the key listener

    def watchdog():
        misses = 0
        while True:
            time.sleep(0.1)
            if not state["live"]:
                misses = 0
                if state["recording"] and time.monotonic() - ui["press_t"] > 6:
                    # A start that never finished (for example a hung audio call): reset so the next press works.
                    ui["gen"] += 1
                    state["recording"], state["held"], rec["latched"] = False, False, False
                    say("a recording start stalled, reset so the next press works")
                continue
            if rec["latched"]:
                now, quiet = time.monotonic(), state["silence"]
                if quiet and rec["voiced_secs"] >= 0.3 and now - rec["last_voiced"] > quiet:
                    finish(f"{quiet:g}s of silence, sending")
                elif quiet and rec["voiced_secs"] < 0.3 and now - rec["t0"] > 10:
                    conv["active"] = False
                    frames = list(rec["frames"])
                    peak = float(np.abs(np.concatenate(frames)).max()) if frames else 0.0
                    finish(f"no speech heard in {now - rec['t0']:.0f}s (loudest sound {peak:.3f}), closing the microphone", discard=True)
                elif now - rec["t0"] > MAX_LATCHED:
                    finish(f"recording ran past {MAX_LATCHED} seconds, finishing it")
                continue
            misses = 0 if key_is_down() else misses + 1
            if misses >= 3:
                finish("key release was missed, finishing the recording")
            elif time.monotonic() - rec["t0"] > MAX_HOLD:
                finish(f"recording ran past {MAX_HOLD} seconds, finishing it")

    threading.Thread(target=watchdog, daemon=True).start()
    keyboard.Listener(on_press=on_press, on_release=on_release, daemon=True).start()


TALK_KEYS = ["f18", "cmd_r", "alt_r", "ctrl_r"]  # f18 pairs with the optional Karabiner rule
SPEAKERS = ["af_heart", "af_bella", "af_nicole", "bf_emma", "am_michael"]  # Kokoro's best-graded voices


# --- control socket ---------------------------------------------------------------------------
def status():
    follow_name = names.get(state["current"], "none yet")
    return (f"mode: {state['mode']}\nspeech-to-text: {state['stt']}\nvoice: {'muted' if state['muted'] else 'on'}\n"
            f"talk key: {state['key']}\npause other audio while dictating: {'yes' if state['pause'] else 'no'}\nvoice speed: {state['speed']:g}x\nspeaker: {state['speaker']}\nmicrophone: {state['mic']}\nhands-free conversation: {'armed' if state['conversation'] else 'off'}\nstart sound: {state['cue']}\nstop double-tap recording after silence: {str(state['silence']) + ' s' if state['silence'] else 'off (tap to stop)'}\nlongest spoken reply: {speak.MAX_WORDS} words\npress Enter after local dictation: {'yes' if state['enter'] else 'no'}\nfollowing: {follow_name}")


def command(line):
    parts = line.strip().lower().split()
    if not parts:
        return status()
    cmd, arg = parts[0], parts[1] if len(parts) > 1 else None
    if cmd == "ping":
        return "pong"
    if cmd == "mode" and arg in ("local", "wispr"):
        state["mode"] = arg
        sync_karabiner(arg == "local")
    elif cmd == "stt" and arg in ("parakeet", "whisper"):
        state["stt"] = arg
        preload(arg)
    elif cmd == "enter" and arg in ("on", "off"):
        state["enter"] = arg == "on"
    elif cmd == "mute":
        state["muted"] = True
        speak.cancel()
    elif cmd == "unmute":
        state["muted"] = False
    elif cmd == "cue" and arg:
        try:
            state["cue"] = build_tick(arg)
        except ValueError as e:
            return str(e)
        tick()  # play it so you can hear the choice
    elif cmd == "speed" and arg:
        try:
            state["speed"] = min(1.6, max(0.7, float(arg)))
        except ValueError:
            return "usage: speed 1.0 (normal), 1.15, 1.3 ..."
        speak.SPEED = state["speed"]
    elif cmd == "speaker" and arg:
        if arg not in SPEAKERS:
            return "speaker must be one of: " + ", ".join(SPEAKERS)
        state["speaker"] = speak.VOICE = arg
        speak.cancel()
        threading.Thread(target=lambda: speak.speak("This is my voice."), daemon=True).start()
    elif cmd == "focus" and arg:
        # The Orca panel reports the focused workspace; follow its most recently active Claude session.
        for attempt in range(3):  # the Orca CLI occasionally fails once; try again before giving up
            try:
                # Orca names a workspace two ways: "<repo id>::<path>" (what sessions carry) and
                # "identity:wt2:local:<id>" (what the focus event may carry). Resolve either to the first.
                wid = arg
                for w in orca("worktree", "list")["worktrees"]:
                    ident = ((w.get("identity") or {}).get("key") or "").lower()
                    if arg == w["id"].lower() or (ident and ident in arg):
                        wid = w["id"].lower()
                        break
                mine = [a for a in claude_sessions() if (a.get("worktreeId") or "").lower() == wid]
                break
            except Exception as e:
                say(f"focus lookup failed ({e}), attempt {attempt + 1}")
                time.sleep(0.5)
        else:
            return "focus failed"
        if not mine:
            state["focus"] = wid  # follow the first session that opens here
            say(f"focus {arg}: no Claude session in that workspace yet, waiting for one")
        elif any(a["orcaSessionId"] == state["current"] for a in mine):
            state["focus"] = None  # already following one of this workspace's sessions
        else:
            pick = max(mine, key=lambda a: a.get("updatedAt", 0))["orcaSessionId"]
            state["current"], state["focus"] = pick, None
            speak.cancel()
            say(f"now following: {names.get(pick, pick)}")
    elif cmd == "doctor":
        import checks
        return checks.report()
    elif cmd == "key" and arg:
        if arg not in TALK_KEYS:
            return "key must be one of: " + ", ".join(TALK_KEYS)
        state["key"] = arg
        save_settings()
        return f"talk key set to {arg}. Restart the helper to use it."
    elif cmd == "pause" and arg in ("on", "off"):
        state["pause"] = arg == "on"
    elif cmd == "playing":
        return "other audio is playing" if other_audio_playing() else "no other audio is playing"
    elif cmd == "mic" and arg:
        state["mic"] = " ".join(parts[1:])  # a name or part of one, e.g. "studio display", or "default"
    elif cmd == "conversation" and arg in ("on", "off"):
        state["conversation"] = arg == "on"
        if not state["conversation"]:
            conv["active"] = False
    elif cmd == "listen":
        if not (conv["listen"] and conv["listen"]()):
            return "cannot listen now (needs local mode and no recording in progress)"
        if state["conversation"]:
            conv["active"], conv["turns"] = True, 0
    elif cmd == "silence" and arg:
        try:
            state["silence"] = 0.0 if arg == "off" else max(0.5, float(arg))
        except ValueError:
            return "usage: silence SECONDS or silence off"
    elif cmd == "limit" and arg and arg.isdigit():
        speak.MAX_WORDS = int(arg)
    elif cmd == "stop":
        speak.cancel()
    elif cmd == "status":
        pass
    elif cmd == "quit":
        closing.set()
        sync_karabiner(False)  # give right Option back to Wispr Flow
        threading.Timer(0.3, lambda: os._exit(0)).start()
        return "quitting"
    else:
        return "unknown command. Try: mode local|wispr, stt parakeet|whisper, cue NAME, silence SECONDS|off, conversation on|off, listen, mic NAME|default, speed X, speaker NAME, key NAME, doctor, pause on|off, playing, focus WORKSPACE, enter on|off, limit N, mute, unmute, stop, status, quit"
    save_settings()
    return status()


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            self.wfile.write((command(self.rfile.readline().decode()) + "\n").encode())
        except Exception as e:
            self.wfile.write(f"error: {e}\n".encode())


def already_running():
    try:
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(2)
        s.connect(SOCK)
        s.sendall(b"ping\n")
        return s.recv(16).strip() == b"pong"
    except OSError:
        return False


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--key", default=None, help="talk key for this run only (pynput name); normally set with `voice key NAME`")
    p.add_argument("--mode", default=None, choices=["wispr", "local"], help="override the saved mode")
    p.add_argument("--no-hotkey", action="store_true", help="skip the global key listener")
    a = p.parse_args()
    sys.stdout.reconfigure(line_buffering=True)
    if already_running():
        sys.exit("The voice helper is already running. Use `voice status`.")
    load_settings()
    if a.mode:
        state["mode"] = a.mode

    say("loading voice and speech models...")
    speak.model()
    preload(state["stt"])
    talker = Talker()
    talker.start()
    threading.Thread(target=follow, args=(talker,), daemon=True).start()
    try:
        state["cue"] = build_tick(state["cue"])
    except (ValueError, OSError):
        state["cue"] = build_tick("Tink")
    sync_karabiner(state["mode"] == "local")
    threading.Thread(target=keep_karabiner_in_sync, daemon=True).start()
    if not a.no_hotkey:
        run_hotkey(a.key or state["key"])

    os.makedirs(os.path.dirname(SOCK), exist_ok=True)
    if os.path.exists(SOCK):
        os.remove(SOCK)
    server = socketserver.ThreadingUnixStreamServer(SOCK, Handler)
    server.daemon_threads = True
    say(f"ready. mode: {state['mode']}, talk key: {a.key or state['key']}")
    try:
        server.serve_forever()
    finally:
        closing.set()
        sync_karabiner(False)
        if os.path.exists(SOCK):
            os.remove(SOCK)


if __name__ == "__main__":
    main()
