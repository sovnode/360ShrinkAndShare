"""Explorer right-click menu for 360 Video Shrinker. Per user (HKCU), no admin rights needed.

  video files (.mp4 .mov .m4v)  ->  360 Video Shrinker > one entry per preset marked "menu"
                                                       > Open in 360 Video Shrinker...
  folders, and inside a folder  ->  360 Video Shrinker > Find 360 videos in this folder...

On Windows 11 the entries are in the classic menu: "Show more options" (or Shift + right-click).
Explorer starts one pythonw per selected file; app.pyw hands every file to the one open window.
"""
import ctypes
import os
import winreg

import paths

KEY = "Video360Shrinker" + paths.INSTANCE
EXTS = (".mp4", ".mov", ".m4v")
FILE_BASES = [r"Software\Classes\SystemFileAssociations\%s\shell" % e for e in EXTS]
DIR_BASES = [(r"Software\Classes\Directory\shell", "%1"), (r"Software\Classes\Directory\Background\shell", "%V")]
ECF_SEPARATORBEFORE = 0x20


def _cmd(args):
    return '"%s" "%s" %s' % (paths.PYTHONW, os.path.join(paths.APP_DIR, "app.pyw"), args)


def _put(path, values=None, command=None):
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_WRITE) as k:
        for name, value in (values or {}).items():
            if isinstance(value, int):
                winreg.SetValueEx(k, name, 0, winreg.REG_DWORD, value)
            else:
                winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)
    if command is not None:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, path + r"\command", 0, winreg.KEY_WRITE) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, command)


def _delete_tree(path):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ | winreg.KEY_WRITE) as k:
            while True:
                try:
                    sub = winreg.EnumKey(k, 0)
                except OSError:
                    break
                _delete_tree(path + "\\" + sub)
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
    except FileNotFoundError:
        pass


def _notify_shell():
    try:
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)     # SHCNE_ASSOCCHANGED
    except OSError:
        pass


def install(presets):
    uninstall(notify=False)
    icon = paths.ICON if os.path.exists(paths.ICON) else paths.PYTHONW
    menu = [p for p in presets if p.get("menu")]
    for base in FILE_BASES:
        root = base + "\\" + KEY
        _put(root, {"MUIVerb": paths.APP_NAME, "Icon": icon, "SubCommands": "", "MultiSelectModel": "Player"})
        for i, p in enumerate(menu):
            if '"' in p["name"] or "%" in p["name"]:
                continue
            _put(r"%s\shell\%02d" % (root, i), {"MUIVerb": p["name"], "MultiSelectModel": "Player"},
                 _cmd('--enqueue --preset "%s" "%%1"' % p["name"]))
        _put(root + r"\shell\90open", {"MUIVerb": "Open in %s..." % paths.APP_NAME, "MultiSelectModel": "Player",
                                       "CommandFlags": ECF_SEPARATORBEFORE}, _cmd('--add "%1"'))
    for base, arg in DIR_BASES:
        root = base + "\\" + KEY
        _put(root, {"MUIVerb": paths.APP_NAME, "Icon": icon, "SubCommands": ""})
        _put(root + r"\shell\10find", {"MUIVerb": "Find 360 videos in this folder..."}, _cmd('--scan "%s"' % arg))
    _notify_shell()


def uninstall(notify=True):
    for base in FILE_BASES + [b for b, _ in DIR_BASES]:
        _delete_tree(base + "\\" + KEY)
    if notify:
        _notify_shell()


def is_installed():
    try:
        winreg.OpenKey(winreg.HKEY_CURRENT_USER, FILE_BASES[0] + "\\" + KEY).Close()
        return True
    except FileNotFoundError:
        return False


def refresh_if_installed(presets):
    if is_installed():
        install(presets)
