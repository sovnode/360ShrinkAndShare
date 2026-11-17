"""360 Shrink and Share - starts the window. Shortcut: "360 Shrink and Share.lnk" (pythonw app.pyw).

The Explorer right-click menu runs:
  pythonw app.pyw --enqueue --preset NAME FILE    convert FILE with that preset now
  pythonw app.pyw --add FILE                      put FILE in the list, don't start
  pythonw app.pyw --scan FOLDER                   find the 360 videos in FOLDER and list them
Every call drops a small job file into the data folder's queue (see paths.py). The first call opens
the window; later calls see the window's named mutex and leave the job to it (Explorer starts one
process per selected file, and they must not each open a window).
"""
import ctypes
import json
import os
import sys
import time
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import paths  # noqa: E402

MUTEX_NAME = "Local\\Video360Shrinker.Window" + paths.INSTANCE


def parse_job(argv):
    job = {"action": "show", "files": [], "preset": None, "start": False, "time": time.time()}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--enqueue":
            job.update(action="add", start=True)
        elif a == "--add":
            job["action"] = "add"
        elif a == "--scan":
            job["action"] = "scan"
        elif a == "--no-raise":                   # for scripted tests: don't bring the window forward
            job["raise"] = False
        elif a in ("--preset", "--geometry") and i + 1 < len(argv):
            job[a[2:]] = argv[i + 1]
            i += 1
        else:
            job["files"].append(os.path.abspath(a))
        i += 1
    return job


def write_job(job):
    paths.ensure_dirs()
    name = "%d-%d.json" % (time.time_ns(), os.getpid())
    tmp = os.path.join(paths.QUEUE_DIR, name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(job, f)
    os.replace(tmp, os.path.join(paths.QUEUE_DIR, name))


def main():
    job = parse_job(sys.argv[1:])
    write_job(job)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    handle = k32.CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.get_last_error() == 183:            # ERROR_ALREADY_EXISTS: the open window takes the job
        try:                                      # Explorer gave this process the right to come to the
            ctypes.windll.user32.AllowSetForegroundWindow(wintypes.DWORD(0xFFFFFFFF))   # front: pass it on
        except OSError:
            pass
        return
    import gui                                    # the heavy imports happen only in the window process
    gui.run(geometry=job.get("geometry"), mutex=handle)


if __name__ == "__main__":
    main()
