"""360 Shrink and Share - the window (tkinter). Started by app.pyw; see README.md."""
import ctypes
import json
import os
import queue
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import paths
import presets as presetlib
import engine
import contextmenu

VLC_PATHS = [r"C:\Program Files\VideoLAN\VLC\vlc.exe", r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe"]
COLS = [("size", "Size", 75, "e"), ("video", "Video", 190, "w"), ("length", "Length", 60, "e"),
        ("preset", "Preset", 210, "w"), ("status", "Status", 260, "w"), ("result", "Result", 80, "e"),
        ("saved", "Saved", 110, "w")]
FINISHED = ("done", "failed", "skipped", "unusable")
SCAN_MIN_MB = 5                     # "Find 360 videos in this folder" ignores smaller files


def find_vlc():
    return next((p for p in VLC_PATHS if os.path.exists(p)), None)


def load_settings():
    try:
        with open(paths.SETTINGS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(d):
    try:
        paths.ensure_dirs()
        with open(paths.SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
    except OSError:
        pass


def _on_screen(geometry):
    """True when the top-left corner of a saved 'WxH+X+Y' geometry is on a monitor that is connected now."""
    import re
    m = re.match(r"^\d+x\d+([+-]-?\d+)([+-]-?\d+)$", geometry or "")
    if not m:
        return False
    x, y = int(m.group(1).replace("+", "")), int(m.group(2).replace("+", ""))

    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
    try:
        fn = ctypes.windll.user32.MonitorFromPoint
        fn.restype = ctypes.c_void_p
        fn.argtypes = [POINT, ctypes.c_uint]
        return bool(fn(POINT(x + 40, y + 20), 0))                                    # 0 = MONITOR_DEFAULTTONULL
    except (AttributeError, OSError):
        return False


def video_text(info):
    if not info or not info.get("width"):
        return "online-only" if info and info.get("kind") == "online-only" else ""
    codec = {"hevc": "HEVC", "h264": "H.264", "av1": "AV1"}.get(info.get("vcodec"), (info.get("vcodec") or "").upper())
    fps = (" %.0f fps" % info["fps"]) if info.get("fps") else ""
    return "%dx%d %s%s" % (info["width"], info["height"], codec, fps)


# ------------------------------------------------------------------ preset editor


def _readme():
    """README.txt in an installed copy (opens in Notepad), else README.md."""
    for name in ("README.txt",):
        p = os.path.join(paths.APP_DIR, name)
        if os.path.exists(p):
            return p
    return os.path.join(paths.APP_DIR, "README.md")

class PresetEditor(tk.Toplevel):
    FIELD_ORDER = ["name", "description", "output", "interval", "width", "pick", "search", "skip_unchanged",
                   "timing", "hold", "codec", "crf", "encoder", "speed", "keyframe_every", "audio", "jpeg_quality",
                   "video_qp", "html", "menu"]

    def __init__(self, master, items, on_save):
        super().__init__(master)
        self.title("Presets - %s" % paths.APP_NAME)
        self.transient(master)
        self.items = [dict(p) for p in items]
        self.on_save = on_save
        self.cur = None
        self.vars, self.widgets = {}, {}
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        left = ttk.Frame(self, padding=(10, 10, 4, 10))
        left.grid(row=0, column=0, sticky="ns")
        self.lb = tk.Listbox(left, height=18, width=36, exportselection=False, activestyle="none")
        self.lb.pack(fill="both", expand=True)
        self.lb.bind("<<ListboxSelect>>", self._on_select)
        btns = ttk.Frame(left)
        btns.pack(fill="x", pady=(6, 0))
        for text, cmd in (("New", self.new), ("Copy", self.copy), ("Delete", self.delete),
                          ("Up", lambda: self.move(-1)), ("Down", lambda: self.move(1))):
            ttk.Button(btns, text=text, width=6, command=cmd).pack(side="left", padx=1)

        form = ttk.Frame(self, padding=(8, 10, 12, 4))
        form.grid(row=0, column=1, sticky="nsew")
        form.columnconfigure(1, weight=1)
        L = presetlib.LABELS
        spec = {
            "name": ("Name", "entry", None, "shown in the menus; letters, digits, spaces , . ( ) + & ' _ -"),
            "description": ("Description", "entry", None, ""),
            "output": ("Make", "combo", L["output"], ""),
            "interval": ("Interval", "spin", (0.5, 3600, 0.5), "seconds between panoramas (chapters: at most 5)"),
            "width": ("Width", "combo-edit", ["original", "7680", "5760", "3840", "2880", "1920"],
                      "pixels, or original; height follows the video; never enlarged"),
            "pick": ("Pick", "combo", L["pick"], ""),
            "search": ("Search", "combo", L["search"], "Auto = keyframes when there are 2+ per interval"),
            "skip_unchanged": ("Repeats", "check", "Skip a panorama that looks the same as the one before", ""),
            "timing": ("Timing", "combo", L["timing"], ""),
            "hold": ("Seconds each", "spin", (0.5, 600, 0.5), "how long each panorama is shown (1.5 s in the built-in "
                                                               "presets)"),
            "codec": ("Codec", "combo", L["codec"], ""),
            "crf": ("Quality (CRF)", "spin", (0, 63, 1), ""),
            "encoder": ("GPU", "combo", L["encoder"],
                        "the GPU is set to the same picture quality; AV1 is always made on the CPU"),
            "speed": ("Encoder speed", "combo", L["speed"], "CPU encoders only"),
            "keyframe_every": ("Keyframe every", "spin", (1, 600, 1),
                               "panoramas; 1 = every panorama (instant stepping), higher = a little smaller"),
            "audio": ("Sound", "combo", L["audio"], "only with Real time; off in the built-in presets"),
            "jpeg_quality": ("JPEG quality", "spin", (30, 100, 1), "for Photos"),
            "video_qp": ("12 fps video qp", "spin", (10, 51, 1),
                         "for 12 fps video, every frame: 26 = close to the original; 22 = 70 % bigger; lower = better"),
            "html": ("Browser page", "check", "Pack the result into one .html file with its own 360 viewer",
                     "opens in Edge or Chrome; no VLC needed"),
            "menu": ("Right-click menu", "check", "Show this preset in the File Explorer right-click menu", ""),
        }
        self.hints = {}
        for r, key in enumerate(self.FIELD_ORDER):
            label, kind, arg, hint = spec[key]
            ttk.Label(form, text=label + ":").grid(row=r, column=0, sticky="w", padx=(0, 8), pady=2)
            if kind == "entry":
                v = tk.StringVar()
                w = ttk.Entry(form, textvariable=v, width=58)
            elif kind == "combo":
                v = tk.StringVar()
                w = ttk.Combobox(form, textvariable=v, values=list(arg.values()), state="readonly", width=46)
            elif kind == "combo-edit":
                v = tk.StringVar()
                w = ttk.Combobox(form, textvariable=v, values=arg, width=14)
            elif kind == "spin":
                v = tk.StringVar()
                w = ttk.Spinbox(form, textvariable=v, from_=arg[0], to=arg[1], increment=arg[2], width=10)
            else:
                v = tk.BooleanVar()
                w = ttk.Checkbutton(form, text=arg, variable=v)
            w.grid(row=r, column=1, sticky="w", pady=2)
            h = ttk.Label(form, text=hint, foreground="#666666")
            h.grid(row=r, column=2, sticky="w", padx=(8, 0))
            self.hints[key] = h
            self.vars[key], self.widgets[key] = v, w
        for key in ("output", "timing", "codec"):
            self.vars[key].trace_add("write", lambda *a: self._update_states())

        bottom = ttk.Frame(self, padding=(10, 4, 12, 10))
        bottom.grid(row=1, column=0, columnspan=2, sticky="ew")
        ttk.Button(bottom, text="Restore built-in presets", command=self.restore).pack(side="left")
        ttk.Button(bottom, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(bottom, text="Save", command=self.save).pack(side="right", padx=(0, 6))
        self._fill_list(0)
        self.geometry("+%d+%d" % (master.winfo_rootx() + 40, master.winfo_rooty() + 40))
        self.grab_set()
        self.focus_force()

    # list handling
    def _fill_list(self, select):
        self.lb.delete(0, "end")
        for p in self.items:
            self.lb.insert("end", p["name"])
        if self.items:
            select = max(0, min(select, len(self.items) - 1))
            self.lb.selection_clear(0, "end")
            self.lb.selection_set(select)
            self.lb.see(select)
            self._load(select)

    def _on_select(self, _e=None):
        sel = self.lb.curselection()
        if not sel or sel[0] == self.cur:
            return
        self._store()
        self._fill_names()
        self._load(sel[0])

    def _fill_names(self):
        for i, p in enumerate(self.items):
            if self.lb.get(i) != p["name"]:
                self.lb.delete(i)
                self.lb.insert(i, p["name"])

    def _load(self, i):
        self.cur = i
        p = presetlib.normalize(self.items[i])
        for key in self.FIELD_ORDER:
            v = p[key]
            if key in presetlib.LABELS:
                self.vars[key].set(presetlib.LABELS[key][v])
            elif isinstance(self.vars[key], tk.BooleanVar):
                self.vars[key].set(bool(v))
            else:
                self.vars[key].set(presetlib.fmt_num(v) if isinstance(v, float) else str(v))
        self._update_states()

    def _store(self):
        if self.cur is None or self.cur >= len(self.items):
            return
        p = self.items[self.cur]
        for key in self.FIELD_ORDER:
            v = self.vars[key].get()
            if key in presetlib.LABELS:
                v = {lab: val for val, lab in presetlib.LABELS[key].items()}.get(v, v)
            p[key] = v

    def _update_states(self):
        rev = {k: {lab: val for val, lab in presetlib.LABELS[k].items()} for k in ("output", "timing", "codec")}
        out = rev["output"].get(self.vars["output"].get(), "video")
        timing = rev["timing"].get(self.vars["timing"].get(), "real")
        codec = rev["codec"].get(self.vars["codec"].get(), "hevc")
        video = out == "video"
        chapters = out == "chapters"
        for key in ("timing", "codec", "crf", "speed", "keyframe_every"):
            self.widgets[key].configure(state="readonly" if (video and key in ("timing", "codec", "speed")) else
                                        ("normal" if video else "disabled"))
        self.widgets["hold"].configure(state="normal" if video and timing == "hold" else "disabled")
        self.widgets["audio"].configure(state="readonly" if (video and timing == "real") or chapters else "disabled")
        self.widgets["jpeg_quality"].configure(state="normal" if out == "photos" else "disabled")
        for key in ("pick", "search", "skip_unchanged"):
            self.widgets[key].configure(state="disabled" if chapters else ("normal" if key == "skip_unchanged"
                                                                           else "readonly"))
        self.widgets["video_qp"].configure(state="normal" if chapters else "disabled")
        if chapters:
            self.widgets["speed"].configure(state="readonly")
        self.hints["speed"].configure(text="12 fps video: Medium = Quick Sync medium / NVIDIA p7" if chapters
                                      else "CPU encoders only")
        self.widgets["encoder"].configure(state="readonly")
        self.hints["encoder"].configure(
            text="HEVC; Automatic = Intel GPU, then NVIDIA, then the CPU" if chapters else
            "Photos: the GPU reads the video" if not video else
            "AV1 is always made on the CPU" if codec == "av1" else
            "the GPU is set to the same picture quality as the CPU")
        self.hints["crf"].configure(text=presetlib.CRF_HINT.get(codec, "") + "; lower = better and bigger")

    # buttons
    def new(self):
        self._store()
        p = presetlib.normalize({"name": self._unique("New preset")})
        self.items.append(p)
        self._fill_list(len(self.items) - 1)

    def copy(self):
        self._store()
        if self.cur is None:
            return
        p = dict(self.items[self.cur])
        p["name"] = self._unique(str(p["name"]) + " copy")
        self.items.insert(self.cur + 1, p)
        self._fill_list(self.cur + 1)

    def delete(self):
        if self.cur is None or len(self.items) <= 1:
            messagebox.showinfo("Presets", "Keep at least one preset.", parent=self)
            return
        if not messagebox.askyesno("Delete preset", "Delete the preset '%s'?" % self.items[self.cur]["name"],
                                   parent=self):
            return
        del self.items[self.cur]
        i = self.cur
        self.cur = None
        self._fill_list(i)

    def move(self, d):
        self._store()
        i = self.cur
        if i is None or not (0 <= i + d < len(self.items)):
            return
        self.items[i], self.items[i + d] = self.items[i + d], self.items[i]
        self.cur = None
        self._fill_list(i + d)

    def restore(self):
        self._store()
        have = {str(p["name"]).lower() for p in self.items}
        added = [dict(p) for p in presetlib.DEFAULTS if p["name"].lower() not in have]
        self.items += added
        self._fill_list(self.cur or 0)
        messagebox.showinfo("Presets", "Added %d built-in preset(s) that were missing." % len(added), parent=self)

    def _unique(self, base):
        names = {str(p["name"]).lower() for p in self.items}
        name, k = base, 2
        while name.lower() in names:
            name = "%s %d" % (base, k)
            k += 1
        return name

    def save(self):
        self._store()
        try:
            saved = presetlib.save(self.items)
        except ValueError as e:
            messagebox.showerror("Presets", str(e), parent=self)
            return
        self.on_save(saved)
        self.destroy()


# ------------------------------------------------------------------ main window

class MainWindow:
    def __init__(self, root, geometry=None):
        self.root = root
        self.scale = root.winfo_fpixels("1i") / 96.0
        root.title("%s %s" % (paths.APP_NAME, paths.VERSION))
        try:
            root.iconbitmap(default=paths.ICON)
        except tk.TclError:
            pass
        self.settings = load_settings()
        self.explicit_geometry = bool(geometry)          # a placement given on the command line is not saved
        saved = self.settings.get("geometry")
        if not geometry and saved and _on_screen(saved):
            geometry = saved
        root.geometry(geometry or "%dx%d" % (int(1220 * self.scale), int(660 * self.scale)))
        if not self.explicit_geometry and self.settings.get("zoomed"):
            root.state("zoomed")
        root.minsize(int(820 * self.scale), int(420 * self.scale))
        style = ttk.Style(root)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=int(22 * self.scale))
        self.presets = presetlib.load()
        self.events = queue.Queue()
        self.rows, self.order = {}, []
        self.lock = threading.Lock()
        self.cancel = threading.Event()
        self.worker = None
        self.autostart_pending = False
        self.closing = False
        self.probe_q = queue.Queue()
        threading.Thread(target=self._probe_loop, daemon=True).start()
        self.vlc = find_vlc()
        self._build()
        engine.cleanup_work()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(150, self._poll_jobs)
        root.after(100, self._poll_events)

    # ---------------------------------------------------------- layout
    def _build(self):
        r = self.root
        bar = tk.Menu(r)
        m = tk.Menu(bar, tearoff=False)
        m.add_command(label="Add videos...", command=self.add_files, accelerator="Ctrl+O")
        m.add_command(label="Add folder (find 360 videos)...", command=self.add_folder)
        m.add_separator()
        m.add_command(label="Browser page from a 360 video or slideshow (.mp4)...", command=self.page_from_video)
        m.add_command(label="Browser album from a folder of 360 photos...", command=self.page_from_folder)
        m.add_separator()
        m.add_command(label="Remove selected from list", command=self.remove_selected)
        m.add_command(label="Clear finished", command=self.clear_finished)
        m.add_separator()
        m.add_command(label="Exit", command=self.on_close)
        bar.add_cascade(label="File", menu=m)
        m = tk.Menu(bar, tearoff=False)
        m.add_command(label="Edit presets...", command=self.edit_presets)
        bar.add_cascade(label="Presets", menu=m)
        m = tk.Menu(bar, tearoff=False)
        self.menu_var = tk.BooleanVar(value=contextmenu.is_installed())
        m.add_checkbutton(label="Right-click menu in File Explorer", variable=self.menu_var, command=self.toggle_menu)
        m.add_separator()
        m.add_command(label="Check the GPU...", command=self.check_gpu)
        m.add_separator()
        m.add_command(label="Open the conversion log folder", command=lambda: os.startfile(paths.LOG_DIR))
        m.add_command(label="Open the data folder (presets)", command=lambda: os.startfile(paths.DATA_DIR))
        m.add_command(label="Read me", command=lambda: os.startfile(_readme()))
        bar.add_cascade(label="Tools", menu=m)
        r.config(menu=bar)
        r.bind("<Control-o>", lambda e: self.add_files())

        top = ttk.Frame(r, padding=(8, 8, 8, 2))
        top.pack(fill="x")
        ttk.Button(top, text="Add videos...", command=self.add_files).pack(side="left")
        ttk.Button(top, text="Add folder...", command=self.add_folder).pack(side="left", padx=(6, 0))
        ttk.Label(top, text="Preset:").pack(side="left", padx=(18, 4))
        names = [p["name"] for p in self.presets]
        cur = self.settings.get("preset")
        self.preset_var = tk.StringVar(value=cur if cur in names else names[0])
        self.preset_box = ttk.Combobox(top, textvariable=self.preset_var, state="readonly", width=36, values=names)
        self.preset_box.pack(side="left")
        self.preset_box.bind("<<ComboboxSelected>>", lambda e: self._show_preset())
        ttk.Button(top, text="Use for selected", command=self.apply_preset_to_selected).pack(side="left", padx=(6, 0))
        ttk.Button(top, text="Edit presets...", command=self.edit_presets).pack(side="left", padx=(6, 0))
        self.preset_info = ttk.Label(r, padding=(10, 2, 8, 4), foreground="#555555")
        self.preset_info.pack(fill="x")

        out = ttk.Frame(r, padding=(8, 0, 8, 6))
        out.pack(fill="x")
        self.out_mode = tk.StringVar(value=self.settings.get("out_mode", "next"))
        ttk.Radiobutton(out, text="Save next to each original", value="next", variable=self.out_mode).pack(side="left")
        ttk.Radiobutton(out, text="Save in folder:", value="folder", variable=self.out_mode).pack(side="left",
                                                                                               padx=(16, 4))
        self.out_dir = tk.StringVar(value=self.settings.get("out_dir", ""))
        ttk.Entry(out, textvariable=self.out_dir).pack(side="left", fill="x", expand=True)
        ttk.Button(out, text="Browse...", command=self.browse_out).pack(side="left", padx=(6, 0))

        opt = ttk.Frame(r, padding=(8, 0, 8, 6))
        opt.pack(fill="x")
        self.html_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="Browser page (.html): one file that opens in Edge or Chrome with its own 360 "
                                  "viewer, nothing to install", variable=self.html_var,
                        command=self._html_changed).pack(side="left")

        mid = ttk.Frame(r, padding=(8, 0, 8, 0))
        mid.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(mid, columns=[c[0] for c in COLS], selectmode="extended")
        self.tree.heading("#0", text="Video", anchor="w")
        self.tree.column("#0", width=int(300 * self.scale), anchor="w")
        for key, text, width, anchor in COLS:
            self.tree.heading(key, text=text, anchor=anchor)
            self.tree.column(key, width=int(width * self.scale), anchor=anchor, stretch=(key == "status"))
        ysb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ysb.pack(side="left", fill="y")
        self.tree.tag_configure("done", foreground="#1a7f37")
        self.tree.tag_configure("bad", foreground="#b42318")
        self.tree.tag_configure("muted", foreground="#888888")
        self.tree.bind("<Double-1>", self.on_double)
        self.tree.bind("<Button-3>", self.on_right_click)
        self.tree.bind("<Delete>", lambda e: self.remove_selected())
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._show_selected())

        bottom = ttk.Frame(r, padding=8)
        bottom.pack(fill="x")
        self.progress = ttk.Progressbar(bottom, maximum=1000, length=int(240 * self.scale))
        self.progress.pack(side="left")
        self.status = ttk.Label(bottom, text="Add 360 videos (or right-click them in File Explorer), "
                                             "pick a preset, then Start.")
        self.status.pack(side="left", padx=10, fill="x", expand=True)
        self.stop_btn = ttk.Button(bottom, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="right")
        self.start_btn = ttk.Button(bottom, text="Start", command=self.start)
        self.start_btn.pack(side="right", padx=(0, 6))
        self.total_lbl = ttk.Label(bottom, text="")
        self.total_lbl.pack(side="right", padx=12)
        self._show_preset()

    def _preset(self, name):
        try:
            return presetlib.find(name, self.presets)
        except KeyError:
            return None

    def _row_preset(self, row):
        """The row's preset, with the browser page switched on when the row asks for it."""
        p = self._preset(row["preset"])
        return dict(p, html=True) if p and row.get("html") else p

    def _html_changed(self):
        on = self.html_var.get()
        for iid in self.order:
            row = self.rows[iid]
            if row["state"] in ("checking", "waiting", "stopped", "skipped") and not row["queued"] \
                    and not row["autostart"]:
                row["html"] = on
                if row["state"] == "skipped" and row["message"].startswith("already converted"):
                    row["state"], row["message"] = "waiting", ""
                if row["info"] and row["state"] == "waiting":
                    self._apply_probe(iid, row["info"])
                self._refresh(iid)

    def _show_preset(self):
        p = self._preset(self.preset_var.get())
        self.preset_info.configure(text=(presetlib.summary(p) + (". " + p["description"] if p["description"] else ""))
                                   if p else "")

    # ---------------------------------------------------------- rows
    def _add_row(self, path, preset_name=None, autostart=False, info=None):
        path = os.path.abspath(path)
        for iid in self.order:
            row = self.rows[iid]
            if os.path.normcase(row["path"]) == os.path.normcase(path) and row["state"] in ("checking", "waiting",
                                                                                              "running", "stopped"):
                if preset_name and row["state"] != "running":
                    row["preset"] = preset_name
                row["autostart"] = row["autostart"] or autostart
                self._refresh(iid)
                if autostart and row["state"] == "waiting":
                    self._schedule_autostart()
                return iid
        iid = self.tree.insert("", "end", text=os.path.basename(path))
        self.rows[iid] = {"iid": iid, "path": path, "preset": preset_name or self.preset_var.get(), "state": "checking",
                          "info": None, "result": None, "autostart": autostart, "message": "", "queued": False,
                          "allow_download": False, "progress": None, "out_dir": None,
                          # the window's "Browser page" box; a right-click job follows its preset alone
                          "html": bool(self.html_var.get()) and not autostart}
        self.order.append(iid)
        if info is None:
            self.probe_q.put(iid)
        else:
            self._apply_probe(iid, info)
        self._refresh(iid)
        return iid

    def _apply_probe(self, iid, info):
        row = self.rows[iid]
        row["info"] = info
        if info["kind"] in ("360", "online-only"):
            row["state"] = "waiting"
            p = self._row_preset(row)
            if p and info["kind"] == "360" and self.out_mode.get() == "next":
                out = engine.output_path(row["path"], p, info["width"], info["height"])
                if os.path.exists(out):
                    row["state"], row["message"] = "skipped", "already converted with this preset"
        else:
            row["state"], row["message"] = "unusable", info["reason"]

    def _refresh(self, iid):
        row = self.rows.get(iid)
        if not row or not self.tree.exists(iid):
            return
        info = row["info"] or {}
        res = row["result"] or {}
        st = row["state"]
        if st == "checking":
            status = "Checking..."
        elif st == "waiting":
            status = "Waiting" + (" (queued)" if row["queued"] else "")
            if info.get("kind") == "online-only":
                status += " - online-only, converting downloads it"
        elif st == "running":
            status = "Converting %s" % (row["progress"] or "")
        elif st == "done":
            status = "Done" + ((" - " + res["made_on"]) if res.get("made_on") else "") + (
                " - original in Recycle Bin" if row.get("recycled") else "")
        elif st == "stopped":
            status = "Stopped"
        elif st == "unusable":
            status = "Not converted: " + row["message"]
        else:
            status = "%s: %s" % (st.capitalize(), row["message"])
        result = saved = ""
        if st == "done":
            result = engine.human(res.get("output_size"))
            src = res.get("source_size") or 0
            saved = "%s, %s" % (engine.ratio_text(res.get("output_size"), src),
                                engine.human(max(0, src - (res.get("output_size") or 0))))
        elif st == "waiting" and info.get("kind") == "360":
            p = self._row_preset(row)
            est = engine.estimate_output(info, p) if p else None
            result = ("~" + engine.human(est)) if est else ""
        tags = ("done",) if st == "done" else ("bad",) if st == "failed" else ("muted",) if st in ("unusable",
                                                                                                    "skipped") else ()
        self.tree.item(iid, values=(engine.human(info.get("size")) if info.get("size") else "", video_text(info),
                                    engine._mmss(info["duration"]) if info.get("duration") else "", row["preset"],
                                    status, result, saved), tags=tags)

    def _selected(self):
        return [i for i in self.tree.selection() if i in self.rows]

    def _show_selected(self):
        sel = self._selected()
        if len(sel) == 1:
            row = self.rows[sel[0]]
            res = row["result"] or {}
            text = row["path"]
            if res.get("output") and row["state"] == "done":
                text = "Output: " + res["output"]
            elif row["message"]:
                text = row["message"]
            self.status.configure(text=text)

    # ---------------------------------------------------------- adding
    def add_files(self):
        files = filedialog.askopenfilenames(parent=self.root, title="Add 360 videos",
                                            filetypes=[("Videos", "*.mp4 *.mov *.m4v"), ("All files", "*.*")])
        for f in files:
            self._add_row(f)

    def add_folder(self):
        d = filedialog.askdirectory(parent=self.root, title="Find 360 videos in this folder (and its subfolders)")
        if d:
            self._scan_folder(d)

    def _scan_folder(self, folder):
        self.status.configure(text="Looking for 360 videos in %s ..." % folder)

        def work():
            try:
                found = engine.scan([folder], min_mb=SCAN_MIN_MB,
                                    progress=lambda n, p: self.events.put(("scan-progress", folder, n, p)))
                self.events.put(("scan-done", folder, found, None))
            except Exception as e:  # noqa: BLE001
                self.events.put(("scan-done", folder, [], str(e)))
        threading.Thread(target=work, daemon=True).start()

    def _probe_loop(self):
        while True:
            iid = self.probe_q.get()
            row = self.rows.get(iid)
            if not row:
                continue
            try:
                info = engine.probe(row["path"])
            except Exception as e:  # noqa: BLE001
                info = {"path": row["path"], "kind": "unreadable", "reason": str(e), "size": None}
            self.events.put(("probed", iid, info))

    # ---------------------------------------------------------- jobs from the right-click menu
    def _poll_jobs(self):
        try:
            names = sorted(f for f in os.listdir(paths.QUEUE_DIR) if f.endswith(".json"))
        except OSError:
            names = []
        for n in names:
            full = os.path.join(paths.QUEUE_DIR, n)
            try:
                with open(full, encoding="utf-8") as f:
                    job = json.load(f)
            except (OSError, ValueError):
                job = None
            try:
                os.remove(full)
            except OSError:
                pass
            if job:
                self._handle_job(job)
        if not self.closing:
            self.root.after(400, self._poll_jobs)

    def _handle_job(self, job):
        if job.get("raise", True):
            self._raise_window()
        preset = job.get("preset")
        if preset:
            p = self._preset(preset)
            if p is None:
                messagebox.showwarning(paths.APP_NAME, "There is no preset called '%s' any more; the file was "
                                                       "listed with '%s' and not started." % (preset,
                                                                                             self.preset_var.get()))
                job["start"] = False
            preset = p["name"] if p else None
        if job.get("action") == "add":
            for f in job.get("files", []):
                self._add_row(f, preset, autostart=bool(job.get("start")))
        elif job.get("action") == "scan":
            for folder in job.get("files", []):
                self._scan_folder(folder)

    def _raise_window(self):
        r = self.root
        r.deiconify()
        r.lift()
        r.attributes("-topmost", True)
        r.after(300, lambda: r.attributes("-topmost", False))
        r.focus_force()

    def _schedule_autostart(self):
        if not self.autostart_pending:
            self.autostart_pending = True
            self.root.after(700, self._autostart)

    def _autostart(self):
        self.autostart_pending = False
        if any(self.rows[i]["autostart"] and self.rows[i]["state"] == "checking" for i in self.order):
            self._schedule_autostart()                   # wait until the rest of the batch is checked
            return
        rows = [i for i in self.order if self.rows[i]["autostart"] and self.rows[i]["state"] == "waiting"
                and not self.rows[i]["queued"]]
        for i in self.order:
            self.rows[i]["autostart"] = False
        if rows:
            self.start(rows)

    # ---------------------------------------------------------- converting
    def start(self, rows=None):
        if rows is None:
            rows = [i for i in self.order if self.rows[i]["state"] in ("waiting", "stopped") and not self.rows[i]["queued"]]
        if not rows:
            self.status.configure(text="Nothing waiting to convert.")
            return
        out_dir = None
        if self.out_mode.get() == "folder":
            out_dir = self.out_dir.get().strip()
            if not out_dir or not os.path.isdir(out_dir):
                messagebox.showwarning(paths.APP_NAME, "Choose an existing folder to save in, or pick "
                                                       "'Save next to each original'.")
                return
        online = [i for i in rows if (self.rows[i]["info"] or {}).get("kind") == "online-only"
                  and not self.rows[i]["allow_download"]]
        if online:
            total = sum((self.rows[i]["info"] or {}).get("size") or 0 for i in online)
            if messagebox.askyesno(paths.APP_NAME, "%d of these videos are OneDrive online-only files (%s in all).\n\n"
                                   "Converting them downloads them to this PC first, and they then stay on this PC "
                                   "until OneDrive frees the space.\n\nDownload and convert them?"
                                   % (len(online), engine.human(total))):
                for i in online:
                    self.rows[i]["allow_download"] = True
            else:
                for i in online:
                    self.rows[i]["state"], self.rows[i]["message"] = "skipped", "online-only, not downloaded"
                    self._refresh(i)
                rows = [i for i in rows if i not in online]
        with self.lock:
            for i in rows:
                row = self.rows[i]
                if row["state"] == "stopped":
                    row["state"] = "waiting"
                row["queued"], row["out_dir"] = True, out_dir
        for i in rows:
            self._refresh(i)
        if self.worker is None or not self.worker.is_alive():
            self.cancel.clear()
            self.worker = threading.Thread(target=self._work, daemon=True)
            self.worker.start()
        self.stop_btn.configure(state="normal")

    def _work(self):
        while not self.cancel.is_set():
            with self.lock:
                iid = next((i for i in self.order if self.rows[i]["state"] == "waiting" and self.rows[i]["queued"]),
                           None)
                if iid is None:
                    break
                row = self.rows[iid]
                row["state"] = "running"
            self.events.put(("begin", iid))
            p = self._row_preset(row)
            if p is None:
                res = {"status": "failed", "message": "the preset '%s' no longer exists" % row["preset"]}
            else:
                try:
                    res = engine.convert(row["path"], p, out_dir=row["out_dir"], cancel=self.cancel,
                                         progress=lambda f, t, iid=iid: self.events.put(("progress", iid, f, t)),
                                         allow_download=row["allow_download"], info=row["info"])
                except Exception as e:  # noqa: BLE001 - engine.convert reports its own errors; this is a backstop
                    res = {"status": "failed", "message": "%s: %s" % (type(e).__name__, e)}
            self.events.put(("result", iid, res))
        self.events.put(("idle",))

    def stop(self):
        if self.worker and self.worker.is_alive():
            self.cancel.set()
            self.status.configure(text="Stopping after the current step...")
            with self.lock:
                for i in self.order:
                    if self.rows[i]["state"] == "waiting":
                        self.rows[i]["queued"] = False
            for i in self.order:
                self._refresh(i)

    def _poll_events(self):
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev[0]
                if kind == "probed":
                    iid, info = ev[1], ev[2]
                    if iid in self.rows:
                        self._apply_probe(iid, info)
                        self._refresh(iid)
                        if self.rows[iid]["autostart"]:
                            self._schedule_autostart()
                elif kind == "pages":
                    self._pages_done(ev[1], ev[2])
                elif kind == "begin":
                    row = self.rows[ev[1]]
                    row["progress"] = ""
                    self._refresh(ev[1])
                    n = sum(1 for i in self.order if self.rows[i]["queued"] or self.rows[i]["state"] == "running")
                    self.status.configure(text="Converting %s (%d to go)" % (os.path.basename(row["path"]), n))
                elif kind == "progress":
                    iid, frac, text = ev[1], ev[2], ev[3]
                    row = self.rows.get(iid)
                    if row:
                        row["progress"] = "%d%% - %s" % (100 * frac, text)
                        self._refresh(iid)
                        self.progress.configure(value=int(1000 * frac))
                        self.status.configure(text="%s: %s" % (os.path.basename(row["path"]), text))
                elif kind == "result":
                    iid, res = ev[1], ev[2]
                    row = self.rows.get(iid)
                    if row:
                        st = res.get("status")
                        row["result"] = res
                        row["queued"] = False
                        row["state"] = {"done": "done", "skipped": "skipped", "cancelled": "stopped"}.get(st, "failed")
                        row["message"] = res.get("message", "")
                        if st == "done":
                            row["info"] = row["info"] if (row["info"] or {}).get("kind") == "360" else row["info"]
                        self._refresh(iid)
                        self._update_total()
                    self.progress.configure(value=0)
                elif kind == "idle":
                    self.stop_btn.configure(state="disabled")
                    done = sum(1 for i in self.order if self.rows[i]["state"] == "done")
                    self.status.configure(text="Finished. %d converted in this list." % done)
                    if any(self.rows[i]["queued"] and self.rows[i]["state"] == "waiting" for i in self.order):
                        self.start([])                     # rows queued while the worker was finishing
                elif kind == "scan-progress":
                    self.status.configure(text="Looking in %s ... %d videos checked: %s"
                                          % (ev[1], ev[2] + 1, os.path.basename(ev[3])))
                elif kind == "scan-done":
                    self._scan_done(ev[1], ev[2], ev[3])
                elif kind == "gpu-report":
                    self._gpu_done(ev[1])
        except queue.Empty:
            pass
        if not self.closing:
            self.root.after(100, self._poll_events)

    def _scan_done(self, folder, found, error):
        if error:
            self.status.configure(text="Could not search %s: %s" % (folder, error))
            return
        add = [f for f in found if f["kind"] in ("360", "online-only")]
        for info in add:
            self._add_row(info["path"], info=info)
        n360 = [f for f in found if f["kind"] == "360"]
        online = [f for f in found if f["kind"] == "online-only"]
        other = len(found) - len(add)
        self.status.configure(text="%s: %d 360 videos (%s)%s%s." % (
            folder, len(n360), engine.human(sum(f["size"] or 0 for f in n360)),
            ", %d online-only videos listed unchecked" % len(online) if online else "",
            ", %d other videos left out" % other if other else ""))

    def _update_total(self):
        saved = sum(max(0, (self.rows[i]["result"].get("source_size") or 0) - (self.rows[i]["result"].get("output_size") or 0))
                    for i in self.order if self.rows[i]["state"] == "done" and self.rows[i]["result"])
        self.total_lbl.configure(text="Saved so far: %s" % engine.human(saved) if saved else "")

    # ---------------------------------------------------------- list actions
    def apply_preset_to_selected(self, name=None):
        name = name or self.preset_var.get()
        for iid in self._selected():
            row = self.rows[iid]
            if row["state"] in ("checking", "waiting", "stopped", "skipped") and not row["queued"]:
                row["preset"] = name
                if row["state"] == "skipped" and (row["info"] or {}).get("kind") in ("360", "online-only"):
                    self._apply_probe(iid, row["info"])
                self._refresh(iid)

    def remove_selected(self):
        for iid in self._selected():
            if self.rows[iid]["state"] == "running" or self.rows[iid]["queued"]:
                continue
            self.tree.delete(iid)
            self.order.remove(iid)
            del self.rows[iid]

    def clear_finished(self):
        for iid in list(self.order):
            if self.rows[iid]["state"] in FINISHED:
                self.tree.delete(iid)
                self.order.remove(iid)
                del self.rows[iid]

    # ---------------------------------------------------------- browser pages from what is already on disk
    def page_from_video(self):
        files = filedialog.askopenfilenames(parent=self.root, title="360 videos or slideshows to pack into browser pages",
                                            filetypes=[("MP4 video", "*.mp4 *.m4v *.mov")])
        if files:
            self._make_pages(list(files))

    def page_from_folder(self):
        d = filedialog.askdirectory(parent=self.root, title="Folder of 360 photos (.jpg) for a browser album")
        if d:
            self._make_pages([os.path.normpath(d)])

    def _make_pages(self, sources):
        if getattr(self, "page_thread", None) and self.page_thread.is_alive():
            messagebox.showinfo(paths.APP_NAME, "A browser page is still being made. Try again when it is done.")
            return
        import htmlpack
        self.status.configure(text="Making %d browser page%s..." % (len(sources), "" if len(sources) == 1 else "s"))

        def work():
            done, failed = [], []
            for src in sources:
                try:
                    info = htmlpack.pack(src)
                    chk = htmlpack.verify(info["output"], info)
                    if not chk["ok"]:
                        os.remove(info["output"])
                        raise RuntimeError("check failed: " + chk["problem"])
                    done.append(info)
                except Exception as e:  # noqa: BLE001 - shown to the user
                    failed.append("%s: %s" % (os.path.basename(src), e))
            self.events.put(("pages", done, failed))

        self.page_thread = threading.Thread(target=work, daemon=True)
        self.page_thread.start()

    def _pages_done(self, done, failed):
        lines = []
        for info in done:
            what = "%d photos" % info["pieces"] if info["kind"] == "photos" else "video"
            lines.append("%s (%s, %s)" % (os.path.basename(info["output"]), what, engine.human(info["output_size"])))
            for note in [info.get("warning")] + list(info.get("skipped") or [])[:3]:
                if note:
                    lines.append("    note: " + note)
        if failed:
            lines += ["", "Not made:"] + failed
        self.status.configure(text="%d browser page%s made." % (len(done), "" if len(done) == 1 else "s"))
        if done and not failed and len(done) == 1:
            if messagebox.askyesno(paths.APP_NAME, "Made, next to the original:\n\n" + "\n".join(lines)
                                   + "\n\nOpen it in the browser?"):
                os.startfile(done[0]["output"])
        else:
            (messagebox.showwarning if failed else messagebox.showinfo)(
                paths.APP_NAME, ("Made, next to the originals:\n\n" if done else "") + "\n".join(lines))

    def browse_out(self):
        d = filedialog.askdirectory(parent=self.root, title="Save the outputs in")
        if d:
            self.out_dir.set(os.path.normpath(d))
            self.out_mode.set("folder")

    def _output(self, iid):
        res = self.rows[iid]["result"] or {}
        out = res.get("output")
        return out if out and os.path.exists(out) and self.rows[iid]["state"] == "done" else None

    def play(self, iid):
        out = self._output(iid)
        if not out:
            return
        if os.path.isdir(out) or out.lower().endswith(".html"):
            os.startfile(out)                     # a folder, or a browser page in the default browser
        elif self.vlc:
            subprocess.Popen([self.vlc, out])
        else:
            os.startfile(out)

    def on_double(self, event):
        iid = self.tree.identify_row(event.y)
        if iid in self.rows and self._output(iid):
            self.play(iid)

    def on_right_click(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        if iid not in self.tree.selection():
            self.tree.selection_set(iid)
        sel = self._selected()
        out = self._output(iid)
        m = tk.Menu(self.root, tearoff=False)
        if out and os.path.isfile(out):
            m.add_command(label="Open in the browser" if out.lower().endswith(".html") else
                          "Play in VLC" if self.vlc else "Play", command=lambda: self.play(iid))
        if out:
            m.add_command(label="Show the output in its folder",
                          command=lambda: subprocess.Popen('explorer /select,"%s"' % out))
        m.add_command(label="Show the original in its folder",
                      command=lambda: subprocess.Popen('explorer /select,"%s"' % self.rows[iid]["path"]))
        m.add_separator()
        pm = tk.Menu(m, tearoff=False)
        for p in self.presets:
            pm.add_command(label=p["name"], command=lambda n=p["name"]: self.apply_preset_to_selected(n))
        m.add_cascade(label="Set preset", menu=pm)
        m.add_command(label="Remove from list", command=self.remove_selected)
        done = [i for i in sel if self.rows[i]["state"] == "done" and not self.rows[i].get("recycled")]
        if done:
            m.add_separator()
            m.add_command(label="Send the original%s to the Recycle Bin..." % ("s" if len(done) > 1 else ""),
                          command=lambda: self.recycle_originals(done))
        m.tk_popup(event.x_root, event.y_root)

    def recycle_originals(self, iids):
        checks = [(i, engine.recycle_check(self.rows[i]["path"])) for i in iids]
        ok = [(i, c) for i, c in checks if c["ok"]]
        kept = [c for _, c in checks if not c["ok"]]
        if not ok:
            messagebox.showinfo(paths.APP_NAME, "Nothing was moved:\n\n" + "\n".join(
                "%s: %s" % (os.path.basename(c["source"]), c["reason"]) for c in kept))
            return
        total = sum(c["size"] for _, c in ok)
        names = "\n".join("  %s  (%s)" % (os.path.basename(c["source"]), engine.human(c["size"])) for _, c in ok[:12])
        more = "\n  ... and %d more" % (len(ok) - 12) if len(ok) > 12 else ""
        if not messagebox.askyesno(
                "Send originals to the Recycle Bin",
                "Send %d original video%s (%s) to the Recycle Bin?\n\n%s%s\n\nThe 360 slideshows stay where they are. "
                "You can restore the originals from the Recycle Bin." % (
                    len(ok), "s" if len(ok) > 1 else "", engine.human(total), names, more),
                icon="warning", default="no"):
            return
        failed = []
        for i, c in ok:
            r = engine.recycle(c["source"])
            if r["status"] == "recycled":
                self.rows[i]["recycled"] = True
                self._refresh(i)
            else:
                failed.append(os.path.basename(c["source"]))
        msg = "%d original%s sent to the Recycle Bin." % (len(ok) - len(failed), "" if len(ok) - len(failed) == 1 else "s")
        if failed:
            msg += "\n\nStill in place (Windows did not move them):\n" + "\n".join(failed)
        if kept:
            msg += "\n\nKept:\n" + "\n".join("%s: %s" % (os.path.basename(c["source"]), c["reason"]) for c in kept)
        messagebox.showinfo(paths.APP_NAME, msg)

    # ---------------------------------------------------------- GPU check
    def check_gpu(self):
        self.status.configure(text="Checking what the GPUs can do ...")
        threading.Thread(target=lambda: self.events.put(("gpu-report", engine.gpu_report())), daemon=True).start()

    def _gpu_done(self, rep):
        lines = []
        if rep["no_gpu"]:
            lines.append("The GPU is switched off (V360_NO_GPU=1).")
        for r in rep["encoders"]:
            lines.append("%s on the %s: %s" % (r["codec"], r["gpu"], ("works up to %s" % r["largest_tested"])
                                               if r["largest_tested"] else "not available"))
        lines.append("AV1: always made on the CPU")
        lines.append("Reading HEVC on the GPU: %s" % ("yes" if any(rep["hevc_decoders"].values()) else "no"))
        self.status.configure(text="GPU check finished.")
        messagebox.showinfo(paths.APP_NAME, "\n".join(lines) + "\n\nPresets set to 'Automatic' use the Intel GPU "
                            "first (its files are the size of the CPU's), then the NVIDIA GPU (faster, files a little "
                            "bigger), and the CPU for anything a GPU can't do.")

    # ---------------------------------------------------------- presets, menu, closing
    def edit_presets(self):
        PresetEditor(self.root, self.presets, self._presets_saved)

    def _presets_saved(self, saved):
        self.presets = saved
        names = [p["name"] for p in saved]
        self.preset_box.configure(values=names)
        if self.preset_var.get() not in names:
            self.preset_var.set(names[0])
        self._show_preset()
        for i in self.order:
            self._refresh(i)
        try:
            contextmenu.refresh_if_installed(saved)
        except OSError as e:
            messagebox.showwarning(paths.APP_NAME, "The right-click menu could not be updated: %s" % e)

    def toggle_menu(self):
        try:
            if self.menu_var.get():
                contextmenu.install(self.presets)
                messagebox.showinfo(paths.APP_NAME, "In File Explorer, right-click a video (on Windows 11: "
                                                    "Show more options, or Shift + right-click) and choose "
                                                    "%s." % paths.APP_NAME)
            else:
                contextmenu.uninstall()
        except OSError as e:
            messagebox.showerror(paths.APP_NAME, "Could not change the right-click menu: %s" % e)
        self.menu_var.set(contextmenu.is_installed())

    def on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(paths.APP_NAME, "A conversion is running. Stop it and close?"):
                return
            self.cancel.set()
            self.status.configure(text="Stopping...")
            self._close_when_idle()
            return
        self._close()

    def _close_when_idle(self):
        if self.worker and self.worker.is_alive():
            self.root.after(200, self._close_when_idle)
        else:
            self._close()

    def _close(self):
        self.closing = True
        s = {"preset": self.preset_var.get(), "out_mode": self.out_mode.get(), "out_dir": self.out_dir.get(),
             "geometry": self.settings.get("geometry"), "zoomed": self.settings.get("zoomed", False)}
        if not self.explicit_geometry:
            s["zoomed"] = self.root.state() == "zoomed"
            if self.root.state() == "normal":
                s["geometry"] = self.root.geometry()
        save_settings(s)
        self.root.destroy()


def run(geometry=None, mutex=None):
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Video360Shrinker.App" + paths.INSTANCE)
    except (AttributeError, OSError):
        pass
    root = tk.Tk()
    MainWindow(root, geometry)
    root.mainloop()
