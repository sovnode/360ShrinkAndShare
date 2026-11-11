"""Installs the git pre-push hook that runs tools/privacy_check.py (with --history) before every push.

  python tools/install_hooks.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(ROOT, ".git", "hooks", "pre-push")
PY = sys.executable.replace("\\", "/")

with open(HOOK, "w", encoding="utf-8", newline="\n") as f:
    f.write("#!/bin/sh\n# privacy check before publishing (tools/install_hooks.py)\n"
            '"%s" "$(git rev-parse --show-toplevel)/tools/privacy_check.py" --history || {\n'
            '  echo "Push stopped: the privacy check found something (see above)."; exit 1; }\n' % PY)
print("pre-push hook installed:", HOOK)
