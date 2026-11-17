"""360 Shrink and Share - per-user installer and uninstaller (no admin rights).

Run by Install.cmd (from the unzipped package) and by Uninstall.cmd (in the installed folder), with the
Python that ships in the package. Standard library only.

  setup.py install   [--dir DIR] [--data-dir DIR] [--shortcut-dir DIR] [--no-desktop] [--instance NAME] [--quiet]
  setup.py uninstall [--dir DIR] [--remove-data | --keep-data] [--quiet]

Installed:   %LOCALAPPDATA%\\Programs\\360 Shrink and Share\\  (python\\, app\\, Uninstall.cmd, install-record.json)
Data:        %LOCALAPPDATA%\\360 Shrink and Share\\             (presets, settings, conversion log, work folder)
Shortcuts:   Start menu and Desktop "360 Shrink and Share"
Explorer:    right-click menu (HKCU\\Software\\Classes, per user)
Apps list:   HKCU\\...\\Uninstall\\Video360Shrinker (Settings > Apps > Installed apps can uninstall it)
--instance is for testing beside a real install: it renames the menu key, window and Apps-list entry.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import winreg

APP_NAME = "360 Shrink and Share"
OLD_NAME = "360 Video Shrinker"                 # the name until 1.2
VERSION = "1.3"
HERE = os.path.dirname(os.path.abspath(__file__))           # payload\ in the package
LOCAL = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
DEFAULT_DIR = os.path.join(LOCAL, "Programs", APP_NAME)
DEFAULT_DATA = os.path.join(LOCAL, APP_NAME)
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Video360Shrinker"
RECORD = "install-record.json"
NO_WINDOW = 0x08000000


def say(msg, quiet=False):
    if not quiet:
        print(msg)


def ps(command):
    """Run a short PowerShell command (-Command, so no script-file execution policy applies)."""
    return subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                          capture_output=True, text=True, creationflags=NO_WINDOW)


def ps_quote(s):
    return "'" + s.replace("'", "''") + "'"


def start_menu_dir():
    return os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs")


def desktop_dir():
    r = ps("[Environment]::GetFolderPath('Desktop')")
    d = r.stdout.strip()
    return d if d and os.path.isdir(d) else os.path.join(os.path.expanduser("~"), "Desktop")


def make_shortcut(lnk, target, args, workdir, icon):
    cmd = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut(%s);$s.TargetPath=%s;$s.Arguments=%s;"
           "$s.WorkingDirectory=%s;$s.IconLocation=%s;$s.Description=%s;$s.Save()"
           % (ps_quote(lnk), ps_quote(target), ps_quote(args), ps_quote(workdir), ps_quote(icon),
              ps_quote("Shrink 360 videos and share them as slideshows, photos or browser pages")))
    r = ps(cmd)
    if r.returncode != 0 or not os.path.exists(lnk):
        raise RuntimeError("Could not make the shortcut %s: %s" % (lnk, (r.stderr or "").strip()[:300]))


def unblock(root):
    """Remove the 'downloaded from the internet' mark (Zone.Identifier) from every installed file."""
    n = 0
    for dirpath, _, files in os.walk(root):
        for f in files:
            try:
                os.remove(os.path.join(dirpath, f) + ":Zone.Identifier")
                n += 1
            except OSError:
                pass
    return n


def run_app(target, *args, quiet=True):
    py = os.path.join(target, "python", "python.exe")
    r = subprocess.run([py, os.path.join(target, "app", "shrink360.py")] + list(args),
                       capture_output=True, text=True, creationflags=NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError("shrink360.py %s failed:\n%s%s" % (" ".join(args), r.stdout, r.stderr))
    return r.stdout


def app_running(target):
    """True if a pythonw/python from this install is running (the window is open)."""
    r = ps("@(Get-Process python,pythonw -ErrorAction SilentlyContinue | Where-Object { $_.Path -like %s "
           "-and $_.Id -ne %d }).Count" % (ps_quote(os.path.join(target, "python", "*")), os.getpid()))
    try:
        return int(r.stdout.strip() or 0) > 0
    except ValueError:
        return False


def uninstall_key(instance):
    return UNINSTALL_KEY + instance


def register_apps_entry(target, instance, size_kb):
    key = uninstall_key(instance)
    unins = os.path.join(target, "Uninstall.cmd")
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_WRITE) as k:
        for name, val in (("DisplayName", APP_NAME + ((" (%s)" % instance) if instance else "")),
                          ("DisplayVersion", VERSION), ("Publisher", "360 Shrink and Share"),
                          ("InstallLocation", target), ("DisplayIcon", os.path.join(target, "app", "icon.ico")),
                          ("UninstallString", 'cmd.exe /c "%s"' % unins), ("InstallDate", time.strftime("%Y%m%d"))):
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, val)
        for name, val in (("NoModify", 1), ("NoRepair", 1), ("EstimatedSize", int(size_kb))):
            winreg.SetValueEx(k, name, 0, winreg.REG_DWORD, val)


def remove_apps_entry(instance):
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, uninstall_key(instance))
    except FileNotFoundError:
        pass


def previous_install(instance, target):
    """An install somewhere else (the folder had the old name, 360 Video Shrinker, until 1.2) -> (folder, its
    install record), or None. Found through the Apps-list entry, which kept its key."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, uninstall_key(instance)) as k:
            loc = winreg.QueryValueEx(k, "InstallLocation")[0]
    except OSError:
        return None
    if not loc or os.path.normcase(os.path.abspath(loc)) == os.path.normcase(target):
        return None
    try:
        with open(os.path.join(loc, RECORD), encoding="utf-8") as f:
            return loc, json.load(f)
    except (OSError, ValueError):
        return None


def folder_size(root):
    total = 0
    for dirpath, _, files in os.walk(root):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
    return total


UNINSTALL_CMD = r"""@echo off
rem 360 Shrink and Share - uninstall (removes the right-click menu, shortcuts and this folder)
"%~dp0python\python.exe" "%~dp0app\setup.py" uninstall --dir "%~dp0." %*
if errorlevel 1 (
  echo.
  echo Uninstall stopped. Nothing more was removed.
  pause
  exit /b 1
)
cd /d "%TEMP%"
echo Removing "%~dp0" ...
echo 360 Shrink and Share is uninstalled.
ping -n 3 127.0.0.1 >nul
(goto) 2>nul & rmdir /s /q "%~dp0"
"""


def install(a):
    target = os.path.abspath(a.dir)
    data = os.path.abspath(a.data_dir)
    src_python = os.path.join(HERE, "python")
    src_app = os.path.join(HERE, "app")
    for p in (src_python, src_app):
        if not os.path.isdir(p):
            raise RuntimeError("The package is incomplete (%s is missing). Unzip the whole package and try again." % p)
    say("Installing %s %s" % (APP_NAME, VERSION), a.quiet)
    prev = previous_install(a.instance, target)
    if prev:
        if app_running(prev[0]):
            raise RuntimeError("%s is open. Close its window and run Install.cmd again." % OLD_NAME)
        old_data = prev[1].get("data_dir")
        if a.data_dir == DEFAULT_DATA and old_data and os.path.isdir(old_data):
            data = os.path.abspath(old_data)             # the presets and the log stay where they are
        say("  replacing the earlier install in %s (your presets and log are kept)" % prev[0], a.quiet)
    say("  program: %s" % target, a.quiet)
    say("  your presets and log: %s" % data, a.quiet)
    if os.path.isdir(target):
        if app_running(target):
            raise RuntimeError("360 Shrink and Share is open. Close its window and run Install.cmd again.")
        old = os.path.join(target, "app", "install.json")
        if os.path.exists(old):
            say("  an earlier install is there: replacing the program (your presets and log are kept)", a.quiet)
        for sub in ("python", "app"):
            shutil.rmtree(os.path.join(target, sub), ignore_errors=False) if os.path.isdir(os.path.join(target, sub)) else None
    os.makedirs(target, exist_ok=True)
    os.makedirs(data, exist_ok=True)
    say("  copying files ...", a.quiet)
    shutil.copytree(src_python, os.path.join(target, "python"))
    shutil.copytree(src_app, os.path.join(target, "app"))
    shutil.copy2(os.path.abspath(__file__), os.path.join(target, "app", "setup.py"))
    with open(os.path.join(target, "app", "install.json"), "w", encoding="utf-8") as f:
        json.dump({"data_dir": data, "pylib": "", "instance": a.instance, "version": VERSION}, f, indent=2)
    with open(os.path.join(target, "Uninstall.cmd"), "w", encoding="ascii", newline="\r\n") as f:
        f.write(UNINSTALL_CMD)
    unblock(target)

    say("  checking the program starts ...", a.quiet)
    py = os.path.join(target, "python", "python.exe")
    r = subprocess.run([py, "-c", "import tkinter, numpy, PIL, av; tkinter.Tcl(); print(av.__version__)"],
                       capture_output=True, text=True, creationflags=NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError("The program's Python does not start here:\n" + r.stderr)
    run_app(target, "presets")                              # writes the built-in presets into the data folder

    pyw = os.path.join(target, "python", "pythonw.exe")
    appw = '"%s"' % os.path.join(target, "app", "app.pyw")
    icon = os.path.join(target, "app", "icon.ico")
    name = APP_NAME + ((" (%s)" % a.instance) if a.instance else "") + ".lnk"
    shortcuts = []
    sdirs = [a.shortcut_dir] if a.shortcut_dir else [start_menu_dir()] + ([] if a.no_desktop else [desktop_dir()])
    for d in sdirs:
        os.makedirs(d, exist_ok=True)
        lnk = os.path.join(d, name)
        make_shortcut(lnk, pyw, appw, os.path.join(target, "app"), icon)
        shortcuts.append(lnk)
        say("  shortcut: %s" % lnk, a.quiet)

    menu = not a.no_menu
    if menu:
        run_app(target, "menu", "install")
        say("  right-click menu: added (File Explorer > right-click a video > Show more options)", a.quiet)
    register_apps_entry(target, a.instance, folder_size(target) // 1024)
    if prev:                                             # the earlier install's shortcuts and program folder go
        for lnk in prev[1].get("shortcuts", []):
            if lnk not in shortcuts:
                try:
                    os.remove(lnk)
                except OSError:
                    pass
        shutil.rmtree(prev[0], ignore_errors=True)
    with open(os.path.join(target, RECORD), "w", encoding="utf-8") as f:
        json.dump({"version": VERSION, "dir": target, "data_dir": data, "instance": a.instance,
                   "shortcuts": shortcuts, "menu": menu, "installed": time.strftime("%Y-%m-%d %H:%M:%S")}, f, indent=2)
    say("", a.quiet)
    say("Done. Start it from the Start menu or the Desktop: \"%s\"." % APP_NAME, a.quiet)
    say("To remove it later: Settings > Apps > Installed apps > %s > Uninstall," % APP_NAME, a.quiet)
    say("or run Uninstall.cmd in %s" % target, a.quiet)


def uninstall(a):
    target = os.path.abspath(a.dir)
    rec_path = os.path.join(target, RECORD)
    try:
        with open(rec_path, encoding="utf-8") as f:
            rec = json.load(f)
    except (OSError, ValueError):
        raise RuntimeError("%s is not a 360 Shrink and Share install (no %s)." % (target, RECORD))
    if app_running(target):
        raise RuntimeError("360 Shrink and Share is open (or converting). Close its window and try again.")
    say("Uninstalling %s from %s" % (APP_NAME, target), a.quiet)
    try:
        run_app(target, "menu", "remove")
        say("  right-click menu removed", a.quiet)
    except RuntimeError as e:
        say("  right-click menu: %s" % e, a.quiet)
    for lnk in rec.get("shortcuts", []):
        try:
            os.remove(lnk)
            say("  removed %s" % lnk, a.quiet)
        except FileNotFoundError:
            pass
    remove_apps_entry(rec.get("instance", ""))
    data = rec.get("data_dir")
    remove_data = a.remove_data
    if data and os.path.isdir(data) and not a.remove_data and not a.keep_data and not a.quiet:
        ans = input("Also delete your presets and conversion log in %s? [y/N] " % data).strip().lower()
        remove_data = ans in ("y", "yes")
    if data and remove_data and os.path.isdir(data):
        shutil.rmtree(data, ignore_errors=True)
        say("  removed %s" % data, a.quiet)
    elif data and os.path.isdir(data):
        say("  kept your presets and log in %s" % data, a.quiet)
    # The program folder itself is removed by Uninstall.cmd after this Python has exited.


def main():
    ap = argparse.ArgumentParser(prog="setup.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("install")
    i.add_argument("--dir", default=DEFAULT_DIR)
    i.add_argument("--data-dir", default=DEFAULT_DATA)
    i.add_argument("--shortcut-dir", default="", help="put the one shortcut here instead of Start menu + Desktop")
    i.add_argument("--no-desktop", action="store_true")
    i.add_argument("--no-menu", action="store_true")
    i.add_argument("--instance", default="", help="testing only: a separate menu key, window and Apps entry")
    i.add_argument("--quiet", action="store_true")
    u = sub.add_parser("uninstall")
    u.add_argument("--dir", default=DEFAULT_DIR)
    g = u.add_mutually_exclusive_group()
    g.add_argument("--remove-data", action="store_true")
    g.add_argument("--keep-data", action="store_true")
    u.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    if a.cmd == "install" and a.instance and not a.instance.isalnum():
        ap.error("--instance: letters and digits only")
    try:
        install(a) if a.cmd == "install" else uninstall(a)
    except (RuntimeError, OSError) as e:
        print("\nERROR: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
