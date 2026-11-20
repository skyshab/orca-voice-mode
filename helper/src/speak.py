"""Speak text with local Kokoro, sentence by sentence.

Text is cleaned for speech, split into sentences, and generated while the
previous sentence is still playing, so the first sound starts after one sentence.
"""
import queue
import tempfile
import subprocess
import shutil
import os
import re
import threading
import time

import numpy as np
import soundfile as sf
import sounddevice as sd
from mlx_audio.tts.utils import load_model

MODEL = "mlx-community/Kokoro-82M-bf16"
VOICE = "af_bella"
LANG = "a"
SPEED = 1.0  # 1.0 is normal pace; voiced.py sets this from its saved settings
MAX_WORDS = 300  # longer replies get cut here with a pointer to the screen

_model = None
_cancel = threading.Event()


def cancel():
    """Stop whatever is being spoken right now."""
    _cancel.set()


def model():
    global _model
    if _model is None:
        _model = load_model(MODEL)
        list(_model.generate(text="Ready.", voice=VOICE, lang_code=LANG))  # warm up
    return _model


def clean(text):
    text = re.sub(r"```.*?```", " ", text, flags=re.S)          # code blocks
    text = re.sub(r"`([^`]*)`", r"\1", text)                     # inline code
    text = re.sub(r"https?://\S+", "a link", text)
    text = re.sub(r"[*_#>]+", "", text)                          # markdown marks
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.M)          # bullets
    text = re.sub(r"\s*[—–]\s*", ", ", text)           # dashes read badly
    text = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]", "", text)  # emoji
    text = re.sub(r"^\s*\d+[.)]\s+", "", text, flags=re.M)             # list numbers
    return re.sub(r"\s+", " ", text).strip()


def sentences(text):
    parts = re.split(r"(?<=[.!?])\s+", text)
    return [p for p in parts if p.strip()]


def prepare(text):
    text = clean(text)
    words = text.split()
    if len(words) > MAX_WORDS:
        cut = " ".join(words[:MAX_WORDS])
        cut = cut[: cut.rfind(".") + 1] or cut
        text = cut + " The rest of the answer is on screen."
    return sentences(text)


def _speak(text, on_first_sound=None):
    """Blocks until playback ends. Returns seconds from call to first sound."""
    m = model()
    _cancel.clear()
    q = queue.Queue(maxsize=2)
    t0 = time.perf_counter()
    first = {"t": None}

    def producer():
        for s in prepare(text):
            if _cancel.is_set():
                break
            for r in m.generate(text=s, voice=VOICE, lang_code=LANG, speed=SPEED):
                if r.audio is not None and np.asarray(r.audio).size:
                    item = (np.asarray(r.audio, dtype=np.float32).reshape(-1), r.sample_rate)
                    while not _cancel.is_set():
                        try:
                            q.put(item, timeout=0.2)
                            break
                        except queue.Full:
                            pass
        q.put(None)

    threading.Thread(target=producer, daemon=True).start()
    # One ffplay process plays the whole reply from a pipe, so sentences run together with no gap. It uses the
    # current default output (PortAudio only lists devices once) and cannot hang this process; killing it cancels.
    out_sr = 24000  # Kokoro's sample rate
    proc = subprocess.Popen(["ffplay", "-nodisp", "-autoexit", "-loglevel", "error", "-f", "f32le", "-ar", str(out_sr),
                             "-ch_layout", "mono", "-i", "pipe:0"],
                            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        while (item := q.get()) is not None:
            if _cancel.is_set():
                continue  # drain the queue so the producer can finish
            audio, sr = item
            if first["t"] is None:
                first["t"] = time.perf_counter() - t0
                if on_first_sound:
                    on_first_sound(first["t"])
            data = np.concatenate([audio, np.zeros(int(sr * 0.12), dtype=np.float32)]).astype("float32")  # short pause
            step = sr // 10
            for i in range(0, len(data), step):  # small pieces so cancel() takes effect within about a tenth of a second
                if _cancel.is_set():
                    break
                try:
                    proc.stdin.write(data[i:i + step].tobytes())
                    proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    _cancel.set()
                    break
        try:
            proc.stdin.close()
        except OSError:
            pass
        while proc.poll() is None:  # let the last sentence finish playing
            if _cancel.is_set():
                break
            time.sleep(0.03)
    finally:
        if proc.poll() is None:
            proc.kill() if _cancel.is_set() else proc.terminate()
    return first["t"]


_active = threading.Event()
_was_cancelled = False


def speak(text, on_first_sound=None):
    """Blocks until playback ends. Returns seconds from call to first sound."""
    global _was_cancelled
    _active.set()
    try:
        return _speak(text, on_first_sound)
    finally:
        _was_cancelled = _cancel.is_set()
        _active.clear()


def last_cancelled():
    """True if the most recent reply was cut off by cancel() rather than finishing."""
    return _was_cancelled


def wait_idle(timeout=1.0):
    """Wait until any speech has stopped and released the audio device (after cancel())."""
    t0 = time.monotonic()
    while _active.is_set() and time.monotonic() - t0 < timeout:
        time.sleep(0.02)


if __name__ == "__main__":
    import sys
    print(f"first sound after {speak(' '.join(sys.argv[1:]) or 'This is the Kokoro voice, reading a test sentence.'):.2f}s")
