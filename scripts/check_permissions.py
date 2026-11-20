"""`voice doctor` without a running helper: run the same checks from this terminal. macOS gives permissions to
the app that starts the helper, so for the true answer ask the running helper (`voice doctor` does that first)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "helper", "src"))
import checks

print(checks.report())
print("\nNote: this ran from this terminal. The helper uses the permissions of the app that starts it (Orca, when started from the panel).")
