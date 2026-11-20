"""Check what the helper needs from macOS and from the machine. Run inside the helper process, because macOS
grants microphone, key-listening and paste permissions to the app that started it, so only the helper itself
can say what it is allowed to do."""
import os
import platform
import shutil

from orca_cli import find_orca


def _quartz():
    try:
        import Quartz
        return Quartz
    except ImportError:
        return None


def _microphone():
    try:
        import AVFoundation
        status = AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(AVFoundation.AVMediaTypeAudio)
        return {0: ("ask", "not asked yet. It will ask the first time dictation starts."),
                1: ("bad", "restricted by a profile or parental controls."),
                2: ("bad", "denied. Allow it in System Settings, Privacy & Security, Microphone."),
                3: ("ok", "allowed.")}.get(int(status), ("ask", "unknown"))
    except Exception:
        return ("ask", "could not be checked here. If dictation records silence, allow it in System Settings, Privacy & Security, Microphone.")


def run_checks():
    """Return a list of (level, name, message) with level ok, ask or bad."""
    rows = []
    mac = platform.system() == "Darwin"
    rows.append(("ok" if mac else "bad", "macOS", "yes." if mac else "this only runs on a Mac."))
    arm = platform.machine() == "arm64"
    rows.append(("ok" if arm else "bad", "Apple Silicon", "yes." if arm else "the speech models need an Apple Silicon Mac (M1 or newer)."))
    for tool, why, fix in (("ffplay", "plays the spoken reply", "brew install ffmpeg"),
                           ("uv", "runs the helper", "brew install uv")):
        rows.append(("ok" if shutil.which(tool) or os.path.exists(f"/opt/homebrew/bin/{tool}") else "bad", tool,
                     f"found ({why})." if shutil.which(tool) or os.path.exists(f"/opt/homebrew/bin/{tool}") else f"missing ({why}). Install: {fix}"))
    orca = find_orca()
    rows.append(("ok" if os.path.exists(orca) or shutil.which("orca") else "bad", "Orca command line",
                 "found." if os.path.exists(orca) or shutil.which("orca") else "not found. Install Orca and open it once."))
    q = _quartz()
    if q is None:
        rows.append(("ask", "Key listening", "could not be checked (pyobjc is missing)."))
    else:
        listen = bool(q.CGPreflightListenEventAccess())
        rows.append(("ok" if listen else "bad", "Input Monitoring (talk key)",
                     "allowed." if listen else "not allowed. Add the app that starts the helper in System Settings, Privacy & Security, Input Monitoring."))
        post = bool(q.CGPreflightPostEventAccess())
        rows.append(("ok" if post else "bad", "Accessibility (paste dictation)",
                     "allowed." if post else "not allowed. Add the app that starts the helper in System Settings, Privacy & Security, Accessibility."))
    level, message = _microphone()
    rows.append((level, "Microphone", message))
    return rows


def report():
    rows = run_checks()
    mark = {"ok": "ok     ", "ask": "check  ", "bad": "PROBLEM"}
    lines = [f"{mark[l]} {n}: {m}" for l, n, m in rows]
    bad = sum(1 for l, _, _ in rows if l == "bad")
    lines.append("All good." if not bad else f"{bad} thing(s) need attention.")
    return "\n".join(lines)
