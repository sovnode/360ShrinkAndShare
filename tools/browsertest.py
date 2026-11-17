"""Open the browser pages (htmlpack.py) in headless Edge and Chrome, from disk as a colleague would, and check
that the 360 view really shows the pictures and that the keys and the mouse work.

    python tools\\browsertest.py ALBUM.html|VIDEO.html ... [--shots DIR] [--browser edge|chrome|both]

Exit code 0 = every check passed. Used by selftest.py (which makes small pages first).
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cdp  # noqa: E402
import paths  # noqa: E402

EXES = {"edge": [p for p in cdp.BROWSERS if "msedge" in p], "chrome": [p for p in cdp.BROWSERS if "chrome" in p]}
STATE = ("JSON.stringify({ready: V360.ready, error: V360.error, index: V360.index, yaw: V360.yaw, pitch: V360.pitch,"
         " fov: V360.fov, kind: V360.kind, count: V360.count, frames: V360.frames,"
         " t: V360.video ? V360.video.currentTime : null, paused: V360.video ? V360.video.paused : null,"
         " vw: V360.video ? V360.video.videoWidth : null})")


def state(b):
    return json.loads(b.eval(STATE))


def lit(b):
    """Is there a picture in the view? (the background is #111: a real picture has brighter or varied pixels)"""
    px = [b.eval("V360.sample(%s, %s)" % (x, y)) for x, y in ((0.5, 0.5), (0.25, 0.4), (0.75, 0.6), (0.4, 0.7))]
    return any(max(p) > 40 for p in px) and len({tuple(p) for p in px}) > 1, px


def check_page(b, page, shots=None, tag=""):
    """-> list of (name, ok, detail)."""
    out = []

    def add(name, ok, detail=""):
        out.append((name, bool(ok), str(detail)))

    t0 = time.time()
    b.open(page, wait=0.5)
    try:
        b.wait_for("window.V360 && (V360.ready || V360.error)", timeout=180)
    except TimeoutError as e:
        add("opens", False, e)
        return out
    s = state(b)
    add("opens", s["ready"] and not s["error"], s["error"] or "%.1f s" % (time.time() - t0))
    if not s["ready"]:
        return out
    ok, px = lit(b)
    add("shows a picture", ok, px)
    stem = os.path.splitext(os.path.basename(page))[0][:60]
    if shots:
        b.screenshot(os.path.join(shots, "%s %s 1 opened.png" % (stem, tag)))
    # look around: drag left by 300 px turns the view right; the wheel zooms in
    b.drag(640, 180, 340, 180)
    s2 = state(b)
    add("drag looks around", s2["yaw"] > s["yaw"] + 5, "yaw %.1f -> %.1f" % (s["yaw"], s2["yaw"]))
    b.wheel(640, 180, -400)
    time.sleep(0.2)
    s3 = state(b)
    add("wheel zooms in", s3["fov"] < s2["fov"] - 5, "fov %.1f -> %.1f" % (s2["fov"], s3["fov"]))
    b.wheel(640, 180, 400)
    if s["kind"] == "photos":
        if s["count"] > 1:
            before = b.eval("V360.sample()")
            b.key("ArrowRight", vk=39)
            b.wait_for("V360.index === 1", timeout=30)
            add("Right arrow: next photo", True, "photo 2 of %d" % s["count"])
            add("the picture changed", b.eval("V360.frames") >= 2, (before, b.eval("V360.sample()")))
            b.key("ArrowLeft", vk=37)
            b.wait_for("V360.index === 0", timeout=30)
            add("Left arrow: previous photo", True)
            b.key("End", vk=35)
            b.wait_for("V360.index === %d" % (s["count"] - 1), timeout=60)
            add("End: last photo", lit(b)[0], "photo %d" % s["count"])
            add("the view direction is kept", abs(state(b)["yaw"] - s2["yaw"]) < 0.01)
            b.key("t", vk=84)
            add("T shows the thumbnails", b.eval("document.getElementById('strip').classList.contains('open') && "
                                                 "document.querySelectorAll('#strip img').length") == s["count"])
            time.sleep(0.4)
            if shots:
                b.screenshot(os.path.join(shots, "%s %s 2 last photo.png" % (stem, tag)))
    else:
        add("video size known", s["vw"] and s["vw"] > 0, s["vw"])
        stills = b.eval("!!JSON.parse(document.getElementById('meta').textContent).video.stills")
        if s["count"] > 1:
            b.key("ArrowRight", vk=39)
            b.wait_for("V360.index === 1 && !V360.video.seeking", timeout=30)
            add("Right arrow: next chapter", True, "t = %.2f s" % state(b)["t"])
            b.key("ArrowRight", vk=39)
            b.wait_for("V360.index === 2 && !V360.video.seeking", timeout=30)
            b.key("ArrowLeft", vk=37)
            b.wait_for("V360.index === 1 && !V360.video.seeking", timeout=30)
            add("Left arrow: previous chapter", lit(b)[0], "t = %.2f s" % state(b)["t"])
        f0, t_before, i0 = state(b)["frames"], state(b)["t"], state(b)["index"]
        b.key(" ", code="Space", vk=32)
        try:
            # a slideshow shows a new picture only when the next panorama comes up (every 1.5 s, or 5 s in old ones)
            more = " && V360.index > %d" % i0 if stills and s["count"] > i0 + 1 else ""
            b.wait_for("!V360.video.paused && V360.video.currentTime > %f + 0.8%s" % (t_before, more), timeout=40)
            time.sleep(0.3)
            s4 = state(b)
            add("Space plays", True, "t %.2f -> %.2f s, %d new pictures" % (t_before, s4["t"], s4["frames"] - f0))
            add("pictures reach the 360 view while playing", s4["frames"] > f0 and lit(b)[0])
        except TimeoutError:
            add("Space plays", False, state(b))
        b.key(" ", code="Space", vk=32)
        time.sleep(0.3)
        add("Space pauses", state(b)["paused"])
        if shots:
            b.screenshot(os.path.join(shots, "%s %s 2 playing.png" % (stem, tag)))
    errs = [c for c in b.console() if "EXCEPTION" in c or "error" in c.lower()]
    add("no page errors", not errs, errs[:2])
    return out


def run(pages, browsers=("edge", "chrome"), shots=None, quiet=False):
    """-> (all ok, rows of (browser, page, name, ok, detail))."""
    rows = []
    if shots:
        os.makedirs(shots, exist_ok=True)
    paths.ensure_dirs()
    for name in browsers:
        exe = next((p for p in EXES[name] if os.path.exists(p)), None)
        if not exe:
            rows.append((name, "", "browser installed", None, "not installed: skipped"))
            continue
        with cdp.Browser(exe=exe, profile_parent=paths.WORK_DIR) as b:
            for page in pages:
                b.events.clear()
                for chk, ok, detail in check_page(b, page, shots, name):
                    rows.append((name, os.path.basename(page), chk, ok, detail))
                    if not quiet:
                        print("%-4s %-6s %-44s %s" % ("ok" if ok else "FAIL", name, chk, detail), flush=True)
    return all(r[3] is not False for r in rows), rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pages", nargs="+")
    ap.add_argument("--shots")
    ap.add_argument("--browser", default="both", choices=["edge", "chrome", "both"])
    a = ap.parse_args()
    ok, _ = run(a.pages, ("edge", "chrome") if a.browser == "both" else (a.browser,), a.shots)
    print("ALL OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
