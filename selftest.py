"""Self-test: builds small synthetic 360 clips (including an 8K one) and checks the whole pipeline.

  python selftest.py            (a few minutes; files go to the selftest folder in the data folder)

Checks: file detection (360 tag, 2:1 without a tag, dual fisheye, ordinary 16:9, stereo top-bottom),
sharp-frame picking (only one second in each 5 s window is sharp; the rest is blurred), 8K output in
HEVC / H.264 / AV1 and as JPEGs, real-time length, 360 tags, skipping repeats while the camera stands
still, sound (copied and re-encoded), and peak memory.
1.1: the GPU encoders (NVIDIA and Intel) and GPU decoding, the CPU fallback when a GPU can't do a size or
fails part-way, one chapter per panorama, keyframes, and the HEVC Main profile of x265 slideshows.
1.2: chaptered video (24 fps clip -> 12 fps, a chapter on the one sharp frame of every 2 s, odd frames
included, the chapter frames as keyframes, every frame at one quality setting, sound kept) on every
encoder here.
GPU checks are reported as skipped on a PC without that GPU. Conversions are logged to
selftest\\selftest-log.jsonl, never to the real conversion log.
"""
import ctypes
import math
import os
import shutil
import sys
import time
from ctypes import wintypes
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402
import engine  # noqa: E402
import mp4meta  # noqa: E402
import presets  # noqa: E402

import av  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

DIR = os.path.join(paths.DATA_DIR, "selftest")
OUT = os.path.join(DIR, "out")
FPS = 6
RESULTS = []
SKIPPED = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("  %-4s %s%s" % ("OK" if ok else "FAIL", name, ("  (" + detail + ")") if detail else ""), flush=True)


def skip(name, why):
    SKIPPED.append(name)
    print("  SKIP %s  (%s)" % (name, why), flush=True)


def chapters_of(path):
    with av.open(path) as c:
        return [(float(ch["start"] * ch["time_base"]), ch["metadata"].get("title", "")) for ch in c.chapters()]


def peak_mb():
    class PMC(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("a", ctypes.c_size_t), ("b", ctypes.c_size_t), ("c", ctypes.c_size_t), ("d", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
    k32 = ctypes.WinDLL("kernel32")
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
    pmc = PMC()
    pmc.cb = ctypes.sizeof(PMC)
    k32.K32GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
    return pmc.PeakWorkingSetSize / 2**20


def base_image(w, h, seed=1):
    """Detailed test pattern: grid, lettering, circles (sharp edges everywhere)."""
    small_w, small_h = w // 4, h // 4
    img = Image.new("RGB", (small_w, small_h), (40, 60, 80))
    d = ImageDraw.Draw(img)
    rng = np.random.default_rng(seed)
    try:
        font = ImageFont.truetype(r"C:\Windows\Fonts\arialbd.ttf", max(10, small_h // 40))
    except OSError:
        font = ImageFont.load_default()
    step = max(8, small_w // 96)
    for x in range(0, small_w, step):
        d.line([(x, 0), (x, small_h)], fill=(90, 110, 130), width=1)
    for y in range(0, small_h, step):
        d.line([(0, y), (small_w, y)], fill=(90, 110, 130), width=1)
    for i in range(60):
        x, y = int(rng.integers(0, small_w)), int(rng.integers(0, small_h))
        r = int(rng.integers(step, step * 6))
        d.ellipse([x - r, y - r, x + r, y + r], outline=tuple(int(v) for v in rng.integers(120, 255, 3)), width=2)
    for i in range(160):
        x, y = int(rng.integers(0, small_w - 80)), int(rng.integers(0, small_h - 20))
        d.text((x, y), "TEST %03d ABCD" % i, fill=(240, 240, 220), font=font)
    return np.asarray(img.resize((w, h), Image.NEAREST))


def blurred(a):
    h, w = a.shape[:2]
    return np.asarray(Image.fromarray(a).resize((w // 16, h // 16), Image.BILINEAR).resize((w, h), Image.BILINEAR))


def make_clip(path, w, h, seconds, codec="libx265", sharp_seconds=None, still_from=None, audio=False,
              tag=None, frame_fn=None, pix_fmt="yuv420p", fps=None):
    """Encode a test clip; keyframe every second. sharp_seconds: only these whole seconds are sharp."""
    if os.path.exists(path):
        return path
    FPS = fps or globals()["FPS"]
    sharp = base_image(w, h)
    soft = blurred(sharp)
    oc = av.open(path + ".tmp", "w", format="mp4")
    vst = oc.add_stream(codec, rate=FPS, options={"preset": "ultrafast", "crf": "30"} if codec != "libsvtav1" else {})
    vst.width, vst.height, vst.pix_fmt = w, h, pix_fmt
    vst.codec_context.gop_size = FPS
    vst.codec_context.max_b_frames = 0
    if codec == "libx265":
        vst.codec_context.options = {"preset": "ultrafast", "crf": "30",
                                     "x265-params": "keyint=%d:min-keyint=%d:scenecut=0:bframes=0:log-level=error" % (FPS, FPS)}
        vst.codec_context.codec_tag = "hvc1"
    elif codec == "libx264":
        vst.codec_context.options = {"preset": "ultrafast", "crf": "30",
                                     "x264-params": "keyint=%d:min-keyint=%d:scenecut=0:bframes=0" % (FPS, FPS)}
    ast = None
    if audio:
        ast = oc.add_stream("aac", rate=48000)
        ast.codec_context.layout = "stereo"
        ast.codec_context.format = "fltp"
    for i in range(seconds * FPS):
        t = i / FPS
        if frame_fn is not None:
            rgb = frame_fn(t)
        else:
            sec = int(t)
            src = sharp if (sharp_seconds is None or sec in sharp_seconds) else soft
            shift = int((min(t, still_from) if still_from is not None else t) * w / 40)
            rgb = np.roll(src, shift, axis=1)
        fr = av.VideoFrame.from_ndarray(np.ascontiguousarray(rgb), format="rgb24").reformat(format=pix_fmt)
        fr.pts = i
        for p in vst.encode(fr):
            oc.mux(p)
    for p in vst.encode(None):
        oc.mux(p)
    if ast is not None:
        n = 0
        while n < seconds * 48000:
            k = min(1024, seconds * 48000 - n)
            tt = (np.arange(n, n + k) / 48000.0).astype(np.float32)
            tone = (0.2 * np.sin(2 * math.pi * 440 * tt)).astype(np.float32)
            af = av.AudioFrame.from_ndarray(np.stack([tone, tone]), format="fltp", layout="stereo")
            af.sample_rate = 48000
            af.pts = n
            af.time_base = Fraction(1, 48000)
            for p in ast.encode(af):
                oc.mux(p)
            n += k
        for p in ast.encode(None):
            oc.mux(p)
    oc.close()
    if tag:
        mp4meta.inject_spherical(path + ".tmp", stereo=tag)
    os.replace(path + ".tmp", path)
    return path


def fisheye_frame(w, h):
    a = np.zeros((h, w, 3), np.uint8)
    pat = base_image(h, h, seed=3)
    yy, xx = np.mgrid[0:h, 0:h]
    mask = (xx - h / 2) ** 2 + (yy - h / 2) ** 2 < (h / 2 - 4) ** 2
    for x0 in (0, h):
        a[:, x0:x0 + h][mask] = pat[mask]
    return a


# The presets the checks run with: fixed here, so renaming or removing a preset in the app changes nothing
TEST_PRESETS = {"standard": presets._p(name="standard"),
                "compact": presets._p(name="compact", width=3840),
                "any player": presets._p(name="any player", width=3840, codec="h264", crf=24),
                "photos": presets._p(name="photos", output="photos", width=3840),
                "chapters": presets._p(name="chapters", output="chapters", interval=2.0, speed="medium",
                                       audio="copy")}


def run(src, preset_name, overrides=(), out=OUT):
    p = presets.apply_overrides(TEST_PRESETS[preset_name], list(overrides))
    t = time.time()
    r = engine.convert(src, p, out_dir=out)
    r["wall"] = time.time() - t
    print("     %s: %s  [%s, %.0f s]" % (r["status"], r["message"], os.path.basename(r["output"] or "-"), r["wall"]),
          flush=True)
    return r


def build_clips():
    os.makedirs(DIR, exist_ok=True)
    sharp_secs = {0, 6, 12, 18, 24}                        # one sharp second per 5 s window
    return (make_clip(os.path.join(DIR, "8k_tagged.mp4"), 7680, 3840, 25, sharp_seconds=sharp_secs,
                      still_from=15, tag="mono"),
            make_clip(os.path.join(DIR, "4k_untagged_with_sound.mp4"), 3840, 1920, 12, codec="libx264", audio=True),
            make_clip(os.path.join(DIR, "dual_fisheye.mp4"), 3840, 1920, 4, codec="libx264",
                      frame_fn=lambda t, f=fisheye_frame(3840, 1920): f),
            make_clip(os.path.join(DIR, "ordinary_16x9.mp4"), 1920, 1080, 4, codec="libx264"),
            make_clip(os.path.join(DIR, "stereo_top_bottom.mp4"), 2880, 2880, 6, codec="libx264", tag="top-bottom"),
            make_clip(os.path.join(DIR, "8k_10bit.mp4"), 7680, 3840, 6, tag="mono", pix_fmt="yuv420p10le"),
            make_clip(os.path.join(DIR, "4k_24fps_chapters.mp4"), 3840, 1920, 8, tag="mono", audio=True, fps=24,
                      frame_fn=_chapter_frame()))


CHAPTER_SHARP = (13, 59, 100, 155)                         # the one sharp frame in each 2 s window (24 fps)


def _chapter_frame():
    sharp = base_image(3840, 1920, seed=5)
    soft = blurred(sharp)

    def fn(t):
        i = int(round(t * 24))
        return np.roll(sharp if i in CHAPTER_SHARP else soft, i * 16, axis=1)
    return fn


def main():
    if "--build" in sys.argv:
        build_clips()
        return 0
    os.makedirs(OUT, exist_ok=True)
    for f in os.listdir(OUT):                              # outputs of the last self-test run
        full = os.path.join(OUT, f)
        shutil.rmtree(full) if os.path.isdir(full) else os.remove(full)
    paths.MANIFEST = os.path.join(DIR, "selftest-log.jsonl")   # keep the real conversion log clean
    if os.path.exists(paths.MANIFEST):
        os.remove(paths.MANIFEST)
    print("Building test clips in %s ..." % DIR, flush=True)
    import subprocess                                      # separate process, so the memory check below
    subprocess.run([sys.executable, os.path.abspath(__file__), "--build"], check=True)   # covers conversions only
    clip8k, clip_untagged, clip_fish, clip_flat, clip_stereo, clip10, clip_ch = build_clips()

    print("\nDetection", flush=True)
    kinds = {clip8k: "360", clip_untagged: "360", clip_fish: "fisheye", clip_flat: "not-360", clip_stereo: "360"}
    for clip, want in kinds.items():
        info = engine.probe(clip)
        check("%s -> %s" % (os.path.basename(clip), want), info["kind"] == want, info["reason"])
    info = engine.probe(clip_stereo)
    check("stereo mode read", info["stereo"] == "top-bottom", info["stereo"])

    gpu = {n: engine.gpu_available(n, 7680, 3840)[0] for n in ("hevc_nvenc", "hevc_qsv")}
    hwdec = any(engine.hwdec_available(d) for d in engine.HWDEC_DEVICES)
    print("\nGPU here: %s; HEVC decoding on the GPU: %s" % (
        ", ".join("%s %s" % (n, "yes" if ok else "no") for n, ok in gpu.items()), "yes" if hwdec else "no"), flush=True)

    print("\n8K, sharp-frame picking (photos at full resolution)", flush=True)
    r = run(clip8k, "photos", ["width=original"])
    names = sorted(os.listdir(r["output"])) if r["status"] == "done" else []
    check("photos picked the sharp seconds", [n[:5] for n in names] == ["0000s", "0006s", "0012s", "0018s", "0024s"],
          ", ".join(names))
    if names:
        with Image.open(os.path.join(r["output"], names[0])) as im:
            check("8K JPEG size", im.size == (7680, 3840), "%dx%d" % im.size)
    if hwdec:
        check("photos: the 8K HEVC video was read on the GPU", r.get("decoder") in engine.HWDEC_DEVICES,
              str(r.get("decoder")))
    else:
        skip("photos read on the GPU", "no GPU decoder here")

    print("\nBuilt-in presets: every slideshow shows each panorama 1.5 s (the default)", flush=True)
    vids = [p for p in presets.DEFAULTS if p["output"] == "video"]
    check("built-in slideshow presets are 1.5 s per panorama, whatever the interval",
          all(p["timing"] == "hold" and p["hold"] == 1.5 for p in vids),
          ", ".join("%s: %s %s" % (p["name"].split(" - ")[0], p["timing"], p["hold"]) for p in vids))

    print("\n8K slideshows", flush=True)
    for preset_name, ov, label, want in (("standard", [], "HEVC", "gpu"), ("any player", ["width=original"], "H.264",
                                                                           "libx264"),
                                         ("standard", ["codec=av1", "crf=22"], "AV1", "libsvtav1")):
        r = run(clip8k, preset_name, ov)
        ok = r["status"] == "done"
        check("8K %s slideshow made and checked" % label, ok, r["message"])
        if ok:
            ps = r["settings"]
            step = ps["interval"] if ps["timing"] == "real" else ps["hold"]
            want_len = 25.0 if ps["timing"] == "real" else ps["hold"] * r["frames"]
            c = av.open(r["output"])
            vs = c.streams.video[0]
            size_ok = (vs.codec_context.width, vs.codec_context.height) == (7680, 3840)
            dur = c.duration / 1e6
            c.close()
            check("8K %s keeps 7680x3840 and its length (%s)" % (
                label, "real time" if ps["timing"] == "real" else "%g s per panorama" % ps["hold"]),
                size_ok and abs(dur - want_len) < 0.3, "%.2f s, expected %.2f s" % (dur, want_len))
            sph = mp4meta.read_spherical(r["output"])
            check("8K %s has 360 tags V1+V2" % label, bool(sph["v1"] and sph["v2"]))
            ki = mp4meta.keyframe_info(r["output"])
            check("8K %s: every panorama is a keyframe" % label, ki["keyframes"] == ki["frames"] == r["frames"],
                  "%d of %d" % (ki["keyframes"], ki["frames"]))
            ch = chapters_of(r["output"])
            check("8K %s: one chapter per panorama, at its start, named by its time in the original" % label,
                  [t for _, t in ch] == ["0:00", "0:06", "0:12", "0:18", "0:24"]
                  and [round(s, 2) for s, _ in ch] == [round(k * step, 2) for k in range(5)],
                  ", ".join("%s@%g" % (t, s) for s, t in ch))
            if ps["timing"] == "hold":
                check("8K %s: the name says the time per panorama" % label,
                      ", %gs each)" % ps["hold"] in os.path.basename(r["output"]), os.path.basename(r["output"]))
            if want == "gpu":
                with av.open(r["output"]) as c:
                    prof = c.streams.video[0].codec_context.profile
                check("8K HEVC is HEVC Main profile (hardware decoders play it)", prof == "Main", prof)
                if any(gpu.values()):
                    check("8K HEVC made on the GPU", r.get("encoder") in engine.GPU_ENCODER_NAMES,
                          "%s, read with %s" % (r.get("encoder"), r.get("decoder")))
                    if hwdec:
                        check("8K HEVC source read on the GPU", r.get("decoder") in engine.HWDEC_DEVICES,
                              str(r.get("decoder")))
                else:
                    skip("8K HEVC made on the GPU", "no GPU HEVC encoder here")
            else:
                check("8K %s made on the CPU (no GPU here encodes it at 7680 px)" % label, r.get("encoder") == want,
                      str(r.get("encoder")))

    print("\nGPU choices in a preset", flush=True)
    if gpu["hevc_qsv"]:
        r = run(clip8k, "standard", ["encoder=intel", "width=3840"], out=os.path.join(OUT, "intel"))
        ki = mp4meta.keyframe_info(r["output"]) if r["status"] == "done" else {"keyframes": 0, "frames": -1}
        check("encoder=intel: made on the Intel GPU (Quick Sync), every panorama a keyframe",
              r["status"] == "done" and r.get("encoder") == "hevc_qsv" and ki["keyframes"] == ki["frames"],
              r["message"])
    else:
        skip("encoder=intel", "no Intel Quick Sync HEVC encoder here")
    r = run(clip8k, "standard", ["encoder=cpu", "width=3840"], out=os.path.join(OUT, "cpu"))
    check("encoder=cpu: x265 and software decoding, as in 1.0",
          r["status"] == "done" and r.get("encoder") == "libx265" and r.get("decoder") == "software", r["message"])
    cpu_slideshow = r["output"] if r["status"] == "done" else None
    if cpu_slideshow:
        with av.open(cpu_slideshow) as c:
            prof = c.streams.video[0].codec_context.profile
        ki = mp4meta.keyframe_info(cpu_slideshow)
        check("x265 slideshow is HEVC Main (not Main Intra), every panorama still a keyframe",
              prof == "Main" and ki["keyframes"] == ki["frames"], "%s, %d of %d keyframes" % (prof, ki["keyframes"],
                                                                                         ki["frames"]))
    if engine.gpu_available("h264_nvenc", 1920, 960)[0]:
        r = run(clip8k, "any player", ["width=original", "encoder=nvidia"], out=os.path.join(OUT, "nvidia-h264"))
        check("encoder=nvidia, H.264 at 7680 px: made on the CPU, and the result says why",
              r["status"] == "done" and r.get("encoder") == "libx264" and "4096" in r["message"], r["message"])
    else:
        skip("encoder=nvidia H.264 fallback", "no NVIDIA H.264 encoder here")

    print("\n8K 10-bit HEVC source (as some 8K cameras record)", flush=True)
    info = engine.probe(clip10)
    check("10-bit 8K clip detected as 360", info["kind"] == "360" and "10" in (info["pix_fmt"] or ""),
          "%s %s" % (info["kind"], info["pix_fmt"]))
    r = run(clip10, "standard")
    check("10-bit 8K slideshow made and checked", r["status"] == "done", r["message"])
    if hwdec:
        check("10-bit 8K HEVC read on the GPU", r.get("decoder") in engine.HWDEC_DEVICES, str(r.get("decoder")))
    r = run(clip10, "photos", ["width=original"])
    check("10-bit 8K photos made", r["status"] == "done" and r["frames"] >= 1, r["message"])

    print("\nThorough search (every 0.5 s) picks the same seconds", flush=True)
    r = run(clip8k, "photos", ["width=1920", "search=every"])
    names = sorted(os.listdir(r["output"])) if r["status"] == "done" else []
    check("every-0.5 s search picked the sharp seconds",
          [n[:5] for n in names] == ["0000s", "0006s", "0012s", "0018s", "0024s"], ", ".join(names))

    print("\nRepeats are skipped while the camera stands still (from 15 s on)", flush=True)
    r = run(clip8k, "standard", ["skip_unchanged=true", "width=1920", "timing=real"])
    check("skip unchanged dropped the one repeat (24 s = 18 s)",
          r["status"] == "done" and r.get("skipped_repeats") == 1 and r["frames"] == 4,
          "%s repeats skipped, %s panoramas" % (r.get("skipped_repeats"), r["frames"]))
    if r["status"] == "done":
        c = av.open(r["output"])
        dur = c.duration / 1e6
        c.close()
        check("real time: a skipped repeat keeps the full 25 s timeline", abs(dur - 25) < 0.5, "%.2f s" % dur)
        ch = chapters_of(r["output"])
        check("chapters only for the panoramas kept (no chapter for the repeat)",
              [t for _, t in ch] == ["0:00", "0:06", "0:12", "0:18"], ", ".join(t for _, t in ch))

    print("\nSound and the untagged 2:1 clip", flush=True)
    r = run(clip_untagged, "compact")
    c = av.open(r["output"])
    check("no sound by default", not c.streams.audio)
    c.close()
    check("untagged 2:1 clip gets 360 tags", bool(mp4meta.read_spherical(r["output"])["v1"]))
    r = run(clip_untagged, "compact", ["audio=copy", "interval=4", "timing=real"])
    check("sound copied when a preset asks", r["status"] == "done" and (r.get("check") or {}).get("audio"), r["message"])
    r = run(clip_untagged, "compact", ["audio=speech", "interval=3", "timing=real"])
    check("sound re-encoded (speech) when a preset asks",
          r["status"] == "done" and (r.get("check") or {}).get("audio"), r["message"])
    r = run(clip_untagged, "compact", ["timing=hold", "hold=2"])
    ok = r["status"] == "done"
    if ok:
        c = av.open(r["output"])
        dur = c.duration / 1e6
        c.close()
        ok = abs(dur - 2 * r["frames"]) < 0.2
    check("fixed time per panorama", ok, r["message"])
    if r["status"] == "done":
        ch = chapters_of(r["output"])
        secs = [int(t.split(":")[0]) * 60 + int(t.split(":")[1]) for _, t in ch]
        check("fixed time: a chapter every 2 s, named by its time in the original (window k = 5k..5k+5 s)",
              [round(s, 2) for s, _ in ch] == [0, 2, 4] and all(5 * k <= x < 5 * (k + 1) for k, x in enumerate(secs)),
              ", ".join("%s@%g" % (t, s) for s, t in ch))

    print("\nStereo top-bottom keeps its stereo tag", flush=True)
    r = run(clip_stereo, "compact")
    check("stereo output tagged top-bottom", mp4meta.read_spherical(r["output"])["stereo"] == "top-bottom")

    print("\nChaptered video (whole video at 12 fps, a chapter on the sharpest frame of every 2 s)", flush=True)
    encs = [("auto", None)] + [(e, n) for e, n in (("intel", "hevc_qsv"), ("nvidia", "hevc_nvenc"))
                               if engine.gpu_available(n, 3840, 1920)[0]] + [("cpu", "libx265")]
    for enc, want in encs:
        r = run(clip_ch, "chapters", ["encoder=" + enc], out=os.path.join(OUT, "chapters-" + enc))
        ok = r["status"] == "done"
        check("chapters, encoder=%s: made and checked" % enc, ok, r["message"])
        if not ok:
            continue
        if want:
            check("chapters, encoder=%s: made with %s" % (enc, want), r.get("encoder") == want, str(r.get("encoder")))
        chs = chapters_of(r["output"])
        times = [t for t, _ in chs[1:]]
        check("chapters, encoder=%s: 'Start' at 0, then one on each sharp frame (odd frames too)" % enc,
              len(chs) == 5 and chs[0] == (0.0, "Start") and
              all(abs(t - i / 24.0) < 0.01 for t, i in zip(times, CHAPTER_SHARP)),
              " ".join("%.3f %s" % c for c in chs))
        check("chapters, encoder=%s: 12 fps plus the odd chapter frames (99 of 192)" % enc, r["frames"] == 99,
              str(r["frames"]))
        with av.open(r["output"]) as c:
            vs = c.streams.video[0]
            sizes = [(p.is_keyframe, p.size) for p in c.demux(vs) if p.pts is not None]
            has_audio = bool(c.streams.audio)
            dur = c.duration / 1e6
        keys = [z for k, z in sizes if k]
        other = sorted(z for k, z in sizes if not k)
        check("chapters, encoder=%s: the chapter frames are keyframes" % enc,
              len(keys) >= 4 and min(keys) > other[len(other) // 2],
              "keyframes %d, smallest %d B, median other %d B" % (len(keys), min(keys), other[len(other) // 2]))
        check("chapters, encoder=%s: sound kept, length 8 s" % enc, has_audio and abs(dur - 8) < 0.3,
              "%.2f s" % dur)
    p = presets.normalize(dict(TEST_PRESETS["chapters"], interval=6))
    old = presets.normalize({"name": "old", "output": "chapters", "chapter_qp": 24, "between_qp": 38})
    check("chapters: a preset saved with the two old quality settings loads with one (the chapter one)",
          old["video_qp"] == 24 and "between_qp" not in old, str(old.get("video_qp")))
    check("chapters: interval over 5 s refused", any("interval" in e for e in presets.validate(p)))

    print("\nNever overwrites, never converts non-360", flush=True)
    r = run(clip8k, "standard")
    check("existing output is skipped, not overwritten", r["status"] == "skipped")
    r = run(clip_flat, "standard")
    check("ordinary video is skipped", r["status"] == "skipped", r["message"])

    print("\nA GPU run that fails part-way is redone on the CPU", flush=True)
    if any(gpu.values()):
        real_add = engine._VideoWriter.add

        def flaky(self, pick):
            if self.st.codec_context.name in engine.GPU_ENCODER_NAMES and self.count >= 1:
                raise RuntimeError("simulated GPU failure")
            return real_add(self, pick)
        engine._VideoWriter.add = flaky
        try:
            r = run(clip8k, "standard", ["width=1920"], out=os.path.join(OUT, "gpu-fails"))
        finally:
            engine._VideoWriter.add = real_add
            engine._gpu_state.clear()
        check("GPU failure: redone on the CPU, checked, and the result says so",
              r["status"] == "done" and r.get("encoder") == "libx265" and r.get("made_on") == "CPU"
              and "run failed (RuntimeError: simulated GPU failure) and was redone" in r["message"], r["message"])
    else:
        skip("GPU failure fallback", "no GPU HEVC encoder here")

    peak = peak_mb()
    check("peak memory of the 8K conversions under 3 GB", peak < 3072, "%.0f MB" % peak)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print("\n%d checks, %d failed%s%s" % (len(RESULTS), len(failed), (": " + "; ".join(failed)) if failed else "",
                                         ("; %d skipped (no such GPU here)" % len(SKIPPED)) if SKIPPED else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
