"""Run the Orca command line tool and return its JSON result."""
import json
import os
import shutil
import subprocess

CANDIDATES = (
    "/Applications/Orca.app/Contents/Resources/bin/orca",
    os.path.expanduser("~/Applications/Orca.app/Contents/Resources/bin/orca"),
    "/usr/local/bin/orca",
    "/opt/homebrew/bin/orca",
)


def find_orca():
    return shutil.which("orca") or next((p for p in CANDIDATES if os.path.exists(p)), "orca")


def orca(*args):
    out = subprocess.run([find_orca(), *args, "--json"], capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(out.stderr or out.stdout)
    return json.loads(out.stdout)["result"]
