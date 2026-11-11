"""Commits with a plain UTC date (today at 00:00:00 +0000) for both author and committer, so the public history
shows neither the time zone nor the time of day. Runs the privacy check on the staged files first.

  python tools/commit.py -m "message" [other git commit options]
"""
import datetime
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
check = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "privacy_check.py")])
if check.returncode:
    sys.exit("Not committed: the privacy check found something (see above).")
day = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT00:00:00+0000")
env = dict(os.environ, GIT_AUTHOR_DATE=day, GIT_COMMITTER_DATE=day, TZ="UTC")
sys.exit(subprocess.run(["git", "-C", ROOT, "commit"] + sys.argv[1:], env=env).returncode)
