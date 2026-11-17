"""Where 360 Shrink and Share keeps its program, data and libraries. Light module: no video imports.

The data folder (presets, log, queue, work) is, in order:
  1. the V360_DATA_DIR environment variable, if set;
  2. "data_dir" in install.json beside this file (written by the installer, setup.py);
  3. %LOCALAPPDATA%\\360 Shrink and Share.
A developer copy run from a git checkout can put its own install.json beside this file (it is not
committed). PyAV comes from "pylib" in install.json ("" = none: the packaged Python has it in
site-packages), else from the pylib folder in the data folder, else from the Python's own site-packages.
"""
import json
import os
import sys

APP_NAME = "360 Shrink and Share"
OLD_NAMES = ("360 Video Shrinker",)            # the name until 1.2: its files are still recognised
VERSION = "1.3"

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA_DIR = r"%LOCALAPPDATA%\360 Shrink and Share"


def _install_config():
    try:
        with open(os.path.join(APP_DIR, "install.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


INSTALL = _install_config()
DATA_DIR = os.path.expandvars(os.environ.get("V360_DATA_DIR") or INSTALL.get("data_dir") or DEFAULT_DATA_DIR)
INSTANCE = str(INSTALL.get("instance", ""))     # test installs: a separate menu key and window
PYLIB = os.path.expandvars(INSTALL["pylib"]) if "pylib" in INSTALL else os.path.join(DATA_DIR, "pylib")
PRESETS_FILE = os.path.join(DATA_DIR, "presets.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
QUEUE_DIR = os.path.join(DATA_DIR, "queue")         # jobs from the Explorer right-click menu
WORK_DIR = os.path.join(DATA_DIR, "work")           # outputs are built here, then moved into place
LOG_DIR = os.path.join(DATA_DIR, "log")
MANIFEST = os.path.join(LOG_DIR, "conversions.jsonl")
ICON = os.path.join(APP_DIR, "icon.ico")

PYTHON_DIR = os.path.dirname(sys.executable)
PYTHONW = os.path.join(PYTHON_DIR, "pythonw.exe")
PYTHON = os.path.join(PYTHON_DIR, "python.exe")


def ensure_dirs():
    for d in (DATA_DIR, QUEUE_DIR, WORK_DIR, LOG_DIR):
        os.makedirs(d, exist_ok=True)


def add_pylib():
    if PYLIB and os.path.isdir(PYLIB) and PYLIB not in sys.path:
        sys.path.insert(0, PYLIB)
