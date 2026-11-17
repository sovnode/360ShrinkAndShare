"""360 Shrink and Share - command line (for scripts and bulk runs). The window is app.pyw.

  python shrink360.py presets [--json]
  python shrink360.py probe FILE... [--preset NAME] [--allow-download] [--json]
  python shrink360.py scan FOLDER... [--min-mb 20] [--include-appdata] [--preset NAME] [--report out.csv] [--json]
  python shrink360.py convert FILE... --preset NAME [--html] [--out DIR] [--set key=value ...] [--allow-download] [--quiet] [--json]
  python shrink360.py html PATH... [--out DIR] [--width N|original] [--quality Q] [--json]
        (a 360 .mp4, or a folder of 360 photos, that is already on disk -> one .html browser page beside it)
  python shrink360.py recycle FILE... [--yes] [--json]
  python shrink360.py gpu [--json]                            (what the GPUs here can encode and decode)
  python shrink360.py log [--last N] [--json]
  python shrink360.py menu install|remove|status

FILE may be @list.txt (one path per line). Nothing is ever overwritten; originals are only touched by
'recycle --yes', which sends them to the Recycle Bin and only when the log holds a checked conversion
of exactly that file. Online-only OneDrive files are skipped unless --allow-download is given.
Conversions use the GPU when the preset's encoder allows it (--set encoder=cpu for the CPU only).
--html packs the result of any preset into one .html file that opens in Edge or Chrome with its own 360 viewer
(a video or slideshow plays there without VLC; photos become an album with arrow keys and thumbnails).
"""
import argparse
import csv
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paths  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def _files(items):
    out = []
    for it in items:
        if it.startswith("@"):
            with open(it[1:], encoding="utf-8-sig") as f:
                out += [ln.strip().strip('"') for ln in f if ln.strip() and not ln.lstrip().startswith("#")]
        else:
            out.append(it)
    return out


def _dump(obj):
    print(json.dumps(obj, indent=2, default=str))


def cmd_presets(a):
    import presets
    items = presets.load()
    if a.json:
        return _dump(items)
    for i, p in enumerate(items, 1):
        print("%d. %s%s" % (i, p["name"], "" if p["menu"] else "   (not in right-click menu)"))
        print("     " + presets.summary(p))
        if p["description"]:
            print("     " + p["description"])
    print("\nPresets file: %s" % paths.PRESETS_FILE)


def _info_line(info, est=None):
    import engine
    if info["kind"] in ("online-only", "camera-raw", "unreadable"):
        return "%-11s %9s  %s  [%s]" % (info["kind"], engine.human(info["size"]), info["path"], info["reason"])
    v = "%sx%s %s %.2f fps %s" % (info["width"], info["height"], info["vcodec"], info["fps"] or 0,
                                  engine._mmss(info["duration"]))
    extra = ("  -> about %s" % engine.human(est)) if est else ""
    return "%-11s %9s  %-34s %s  [%s]%s" % (info["kind"], engine.human(info["size"]), v, info["path"],
                                            info["reason"], extra)


def cmd_probe(a):
    import engine
    import presets
    pre = presets.find(a.preset) if a.preset else None
    out = []
    for f in _files(a.files):
        info = engine.probe(f, allow_download=a.allow_download)
        info["estimate"] = engine.estimate_output(info, pre) if pre and info["kind"] == "360" else None
        out.append(info)
        if not a.json:
            print(_info_line(info, info["estimate"]))
    if a.json:
        _dump(out)


def cmd_scan(a):
    import engine
    import presets
    pre = presets.find(a.preset) if a.preset else None
    started = time.time()

    def prog(n, path):
        if not a.json and not a.quiet:
            print("  [%d] %s" % (n + 1, path), file=sys.stderr, flush=True)

    found = engine.scan(a.folders, min_mb=a.min_mb, include_appdata=a.include_appdata, progress=prog)
    for info in found:
        info["estimate"] = engine.estimate_output(info, pre) if pre and info["kind"] == "360" else None
    if a.report:
        cols = ["path", "kind", "reason", "size", "width", "height", "vcodec", "fps", "duration", "tagged",
                "stereo", "online_only", "mtime", "estimate"]
        with open(a.report, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for info in found:
                w.writerow([info.get(c) for c in cols])
    summary = {}
    for info in found:
        s = summary.setdefault(info["kind"], {"files": 0, "bytes": 0, "estimate": 0})
        s["files"] += 1
        s["bytes"] += info["size"] or 0
        s["estimate"] += info.get("estimate") or 0
    if a.json:
        return _dump({"files": found, "summary": summary, "seconds": round(time.time() - started, 1)})
    for info in found:
        print(_info_line(info, info.get("estimate")))
    print("\nSummary (%.0f s):" % (time.time() - started))
    for kind, s in sorted(summary.items()):
        line = "  %-11s %4d files  %10s" % (kind, s["files"], engine.human(s["bytes"]))
        if s["estimate"]:
            line += "  -> about %s with '%s'" % (engine.human(s["estimate"]), pre["name"])
        print(line)
    if a.report:
        print("Report: %s" % a.report)


def cmd_convert(a):
    import engine
    import presets
    pre = presets.apply_overrides(presets.find(a.preset), a.set + (["html=true"] if a.html else []))
    engine.cleanup_work()
    results = []
    files = _files(a.files)
    for i, f in enumerate(files, 1):
        if not a.json:
            print("[%d/%d] %s" % (i, len(files), f), flush=True)
        state = {"last": -1.0}

        def prog(frac, text, _state=state):
            if a.quiet or a.json:
                return
            if frac - _state["last"] >= 0.1 or frac >= 1.0 and _state["last"] < 1.0:
                _state["last"] = frac
                print("    %3.0f%%  %s" % (100 * frac, text), file=sys.stderr, flush=True)

        res = engine.convert(f, pre, out_dir=a.out, progress=prog, allow_download=a.allow_download)
        results.append(res)
        if not a.json:
            print("    %s: %s  (%.0f s)%s" % (res["status"].upper(), res["message"], res["elapsed"],
                                             ("\n    -> " + res["output"]) if res["status"] == "done" else ""),
                  flush=True)
    if a.json:
        _dump(results)
    done = [r for r in results if r["status"] == "done"]
    if not a.json and len(files) > 1:
        src = sum(r["source_size"] or 0 for r in done)
        out = sum(r["output_size"] or 0 for r in done)
        print("\n%d of %d converted: %s -> %s" % (len(done), len(files), engine.human(src), engine.human(out)))
    return 0 if all(r["status"] in ("done", "skipped") for r in results) else 1


def cmd_html(a):
    import engine
    import htmlpack
    width = "auto" if a.width is None else None if a.width.lower() in ("original", "orig", "full") else int(a.width)
    out, bad = [], 0
    for f in _files(a.paths):
        rec = {"source": os.path.abspath(f)}
        try:
            info = htmlpack.pack(f, out_dir=a.out, width=width, quality=a.quality)
            chk = htmlpack.verify(info["output"], info)
            if not chk["ok"]:
                os.remove(info["output"])
                raise RuntimeError("check failed: " + chk["problem"])
            rec.update(status="done", output=info["output"], output_size=info["output_size"], kind=info["kind"],
                       items=info["pieces"] if info["kind"] == "photos" else info.get("chapters"),
                       warning=info.get("warning") or "", skipped=info.get("skipped") or [])
        except Exception as e:  # noqa: BLE001 - reported per file
            rec.update(status="failed", message="%s: %s" % (type(e).__name__, e))
            bad += 1
        out.append(rec)
        if not a.json:
            if rec["status"] == "done":
                what = "%d photos" % rec["items"] if rec["kind"] == "photos" else "video"
                print("DONE    %s, %s\n    -> %s" % (what, engine.human(rec["output_size"]), rec["output"]))
                for note in [rec["warning"]] + rec["skipped"]:
                    if note:
                        print("    note: %s" % note)
            else:
                print("FAILED  %s\n    %s" % (f, rec["message"]))
    if a.json:
        _dump(out)
    return 1 if bad else 0


def cmd_recycle(a):
    import engine
    log = engine.read_log()
    out = []
    for f in _files(a.files):
        chk = engine.recycle_check(f, log)
        if a.yes and chk["ok"]:
            chk = engine.recycle(f)
        else:
            chk["status"] = "would recycle" if chk["ok"] else "kept"
        out.append(chk)
        if not a.json:
            print("%-14s %9s  %s  [%s]" % (chk["status"], engine.human(chk.get("size")), chk["source"], chk["reason"]))
    if a.json:
        _dump(out)
    elif not a.yes and any(c["ok"] for c in out):
        print("\nDry run: nothing was moved. Add --yes to send the listed originals to the Recycle Bin.")


def cmd_gpu(a):
    import engine
    rep = engine.gpu_report()
    if a.json:
        return _dump(rep)
    if rep["no_gpu"]:
        print("The GPU is switched off (V360_NO_GPU=1): everything runs on the CPU.")
    for r in rep["encoders"]:
        state = ("works up to %s (largest size tested)" % r["largest_tested"]) if r["largest_tested"] else \
            ("not available: %s" % r["reason"])
        print("%-5s on the %-10s (%s): %s" % (r["codec"], r["gpu"], r["encoder"], state))
    print("AV1   always on the CPU (SVT-AV1): no GPU here encodes AV1")
    dec = ", ".join("%s %s" % (d, "works" if ok else "not available") for d, ok in rep["hevc_decoders"].items())
    print("HEVC decoding on the GPU: %s" % dec)


def cmd_log(a):
    import engine
    recs = engine.read_log()[-a.last:]
    if a.json:
        return _dump(recs)
    for r in recs:
        if r.get("event") == "recycle":
            print("%s  RECYCLED %s  %s" % (r["time"], engine.human(r.get("size")), r["source"]))
        else:
            print("%s  %-9s %s  [%s] %s" % (r["time"], r["status"].upper(), r["source"], r.get("preset"),
                                           r.get("message", "")))


def cmd_menu(a):
    import contextmenu
    import presets
    if a.action == "install":
        contextmenu.install(presets.load())
        print("Right-click menu installed for %s and folders." % ", ".join(contextmenu.EXTS))
    elif a.action == "remove":
        contextmenu.uninstall()
        print("Right-click menu removed.")
    print("Installed: %s" % contextmenu.is_installed())


def main(argv=None):
    ap = argparse.ArgumentParser(prog="shrink360", description="360 Shrink and Share %s" % paths.VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("presets")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("probe")
    s.add_argument("files", nargs="+")
    s.add_argument("--preset")
    s.add_argument("--allow-download", action="store_true")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("scan")
    s.add_argument("folders", nargs="+")
    s.add_argument("--min-mb", type=float, default=20)
    s.add_argument("--include-appdata", action="store_true")
    s.add_argument("--preset")
    s.add_argument("--report")
    s.add_argument("--quiet", action="store_true")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("convert")
    s.add_argument("files", nargs="+")
    s.add_argument("--preset", required=True)
    s.add_argument("--out")
    s.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    s.add_argument("--allow-download", action="store_true")
    s.add_argument("--html", action="store_true", help="pack the result into one browser page (.html)")
    s.add_argument("--quiet", action="store_true")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("html")
    s.add_argument("paths", nargs="+")
    s.add_argument("--out")
    s.add_argument("--width", help="photos: reduce wider photos to this many pixels (default: 8192; 'original' = never)")
    s.add_argument("--quality", type=int, help="photos that are reduced: JPEG quality (default 85)")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("recycle")
    s.add_argument("files", nargs="+")
    s.add_argument("--yes", action="store_true")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("gpu")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("log")
    s.add_argument("--last", type=int, default=20)
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("menu")
    s.add_argument("action", choices=["install", "remove", "status"])
    a = ap.parse_args(argv)
    return {"presets": cmd_presets, "probe": cmd_probe, "scan": cmd_scan, "convert": cmd_convert,
            "html": cmd_html, "recycle": cmd_recycle, "gpu": cmd_gpu, "log": cmd_log, "menu": cmd_menu}[a.cmd](a) or 0


if __name__ == "__main__":
    sys.exit(main())
