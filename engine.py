"""Conversion engine: look at a video, pick one sharp panorama per interval, write a 360 slideshow MP4
(or a folder of 360 JPEG panoramas), check the result, log it.

Works up to 8K (7680x3840) and beyond: only keyframes are decoded when they are close enough together,
only the current best frame is held in memory, and the encoders run without lookahead so they never buffer
a row of 8K frames.

Since 1.1 the GPU does the heavy work when there is one: HEVC and H.264 are encoded on the NVIDIA (NVENC) or
Intel (Quick Sync) GPU at a quantizer calibrated to give the same picture quality as the CPU encoders, and HEVC
sources are decoded on the GPU. Anything the GPU can't do, or a GPU run that fails or doesn't pass the check,
is redone on the CPU. Every slideshow gets one chapter per panorama, so VLC's Next/Previous chapter keys
(Shift+N / Shift+P) step through the panoramas (VLC 3's frame-by-frame key E stops after a few).
"""
import bisect
import collections
import ctypes
import datetime
import json
import math
import os
import shutil
import tempfile
import time
from ctypes import wintypes
from fractions import Fraction

import paths

paths.add_pylib()
os.environ.setdefault("SVT_LOG", "1")            # SVT-AV1: errors only

import av                    # noqa: E402  (PyAV, from paths.PYLIB)
from av.video.frame import PictureType  # noqa: E402
import numpy as np           # noqa: E402
from PIL import Image        # noqa: E402

import mp4meta               # noqa: E402
import presets as presetlib  # noqa: E402

VIDEO_EXT = {".mp4", ".mov", ".m4v"}
CAMERA_RAW_EXT = {".insv", ".insp", ".lrv", ".360"}
RECALL_ON_DATA_ACCESS = 0x00400000              # OneDrive "online-only": reading downloads it
RECALL_ON_OPEN = 0x00040000
SCORE_W = 640                                   # sharpness is measured on a 640 px wide grey copy
SAMPLE_STEP = 0.5                               # seconds between candidates when every frame is decoded
UNCHANGED_MAD = 2.5                             # mean grey difference (0-255) below which a panorama is a repeat
# Threads: FFmpeg's default decodes one frame per CPU thread at once (one per CPU thread, plus one), which at 8K is several GB.
# A few threads keep memory near 1-2 GB at 8K and cost little speed, because only keyframes are decoded.
DECODE_THREADS = int(os.environ.get("V360_DECODE_THREADS", "4"))
ENCODE_THREADS = int(os.environ.get("V360_ENCODE_THREADS", "4"))
CODECS = {"hevc": "libx265", "h264": "libx264", "av1": "libsvtav1"}          # the CPU encoders
CODEC_LABELS = {"hevc": "HEVC", "h264": "H.264", "av1": "AV1"}
SPEEDS = {"libx264": {"fast": "faster", "medium": "medium", "slow": "slow"},
          "libx265": {"fast": "fast", "medium": "medium", "slow": "slow"},
          "libsvtav1": {"fast": "10", "medium": "8", "slow": "6"}}
# GPU encoders (current NVENC and Quick Sync GPUs encode HEVC up to 8K and H.264 up to 4096 px wide; AV1 is left to the CPU)
GPU_ENCODERS = {("hevc", "nvidia"): "hevc_nvenc", ("hevc", "intel"): "hevc_qsv",
                ("h264", "nvidia"): "h264_nvenc", ("h264", "intel"): "h264_qsv"}
GPU_ENCODER_NAMES = set(GPU_ENCODERS.values())
ENCODER_ORDER = {"nvidia": ("nvidia",), "intel": ("intel",), "cpu": ()}
# encoder = auto: which GPU first. Measured at the same picture quality: Quick Sync HEVC files are the
# size of x265's (within 3 %), NVENC HEVC files 2-12 % bigger (but NVENC is about 3x faster); for H.264 both GPUs
# make files 5-14 % smaller than x264. So Intel first, NVIDIA if the Intel GPU can't, the CPU last.
AUTO_ORDER = {"hevc": ("intel", "nvidia"), "h264": ("intel", "nvidia")}
HW_NAMES = {"nvidia": "NVIDIA GPU", "intel": "Intel GPU"}
ENCODER_LABELS = {"hevc_nvenc": "NVIDIA GPU", "h264_nvenc": "NVIDIA GPU", "hevc_qsv": "Intel GPU",
                  "h264_qsv": "Intel GPU", "libx265": "CPU", "libx264": "CPU", "libsvtav1": "CPU"}
HWDEC_DEVICES = ("cuda", "d3d11va")      # NVIDIA's decoder first, then Windows' D3D11 decoder (any GPU)
HWDEC_CODECS = {"hevc"}                  # 5.7K / 8K HEVC reads about twice as fast; 4K H.264 is as fast in software
NO_GPU = os.environ.get("V360_NO_GPU", "") == "1"      # troubleshooting: everything on the CPU, as in 1.0
QSV_LAMBDA = 118                         # FFmpeg's FF_QP2LAMBDA: Quick Sync takes its quantizer as qp * 118
# GPU quantizer that gives the same picture quality (PSNR-Y) as x265 / x264 at the preset's crf: qp = crf + offset,
# offset by output width and crf. Calibrated on 6 real frames each of three 360 videos (5.7K outdoors,
# 5.7K indoors, 4K indoors; 7680 from the 5.7K frames scaled up), intra-only, x265 'fast' and
# x264 'faster' with the app's settings, NVENC preset p4 and Quick Sync veryslow at constant QP; a straight-line
# fit over crf 24-34 (H.264 18-30). Survey content moves the match by up to about +-2 qp.
GPU_QP_OFFSET = {
    "hevc_nvenc": {2880: {24: -1.2, 27: -0.5, 30: 0.3, 32: 0.8, 34: 1.3},
                   3840: {24: -2.2, 27: -1.4, 30: -0.5, 32: 0.0, 34: 0.6},
                   5760: {24: -2.1, 27: -1.1, 30: -0.1, 32: 0.6, 34: 1.3},
                   7680: {24: -3.1, 27: -2.2, 30: -1.3, 32: -0.7, 34: -0.1}},
    "hevc_qsv": {2880: {24: -4.9, 27: -4.7, 30: -4.6, 32: -4.5, 34: -4.4},
                 3840: {24: -5.5, 27: -5.3, 30: -5.2, 32: -5.1, 34: -5.0},
                 5760: {24: -5.1, 27: -5.1, 30: -5.1, 32: -5.1, 34: -5.1},
                 7680: {24: -5.8, 27: -5.8, 30: -5.8, 32: -5.8, 34: -5.7}},
    "h264_nvenc": {2880: {18: 0.4, 21: 1.5, 24: 2.6, 27: 3.6, 30: 4.7},
                   3840: {18: 0.0, 21: 0.9, 24: 1.8, 27: 2.7, 30: 3.7}},
    "h264_qsv": {2880: {18: -2.7, 21: -2.8, 24: -2.8, 27: -2.9, 30: -2.9},
                 3840: {18: -2.5, 21: -2.7, 24: -2.9, 27: -3.2, 30: -3.4}}}
_gpu_state = {}                          # (encoder, width, height) -> (works, reason), for this session
_hwdec_state = {}                        # decoder device -> works, for this session
GPANO_XMP = ('<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
             '<rdf:Description rdf:about="" xmlns:GPano="http://ns.google.com/photos/1.0/panorama/" '
             'GPano:ProjectionType="equirectangular" GPano:UsePanoramaViewer="True" '
             'GPano:FullPanoWidthPixels="{w}" GPano:FullPanoHeightPixels="{h}" '
             'GPano:CroppedAreaImageWidthPixels="{w}" GPano:CroppedAreaImageHeightPixels="{h}" '
             'GPano:CroppedAreaLeftPixels="0" GPano:CroppedAreaTopPixels="0"/></rdf:RDF></x:xmpmeta>')


class Cancelled(Exception):
    pass


# ------------------------------------------------------------------ looking at a file

def online_only(path):
    try:
        attrs = os.stat(path).st_file_attributes
    except (OSError, AttributeError):
        return False
    return bool(attrs & (RECALL_ON_DATA_ACCESS | RECALL_ON_OPEN))


def _channels(layout):
    if layout is None:
        return None
    n = getattr(layout, "nb_channels", None)
    return n if n is not None else len(getattr(layout, "channels", ()) or ())


def _keyframes(container, stream, limit=None):
    """Decoded frames of the keyframe packets only (fast: the other frames are never decoded)."""
    n = 0
    for pkt in container.demux(stream):
        if pkt.pts is None or not pkt.is_keyframe:
            continue
        for fr in pkt.decode():
            yield fr
            n += 1
            if limit and n >= limit:
                return
    for fr in stream.codec_context.decode(None):
        yield fr


def _looks_dual_fisheye(container, stream):
    """Two unstitched fisheye circles side by side: black corners around both circles."""
    frame = None
    for frame in _keyframes(container, stream, limit=3):
        pass
    if frame is None:
        return False
    g = frame.reformat(width=512, height=256, format="gray").to_ndarray().astype(np.float32)
    corners = []
    for x0 in (0, 256):
        half = g[:, x0:x0 + 256]
        for ys in (slice(0, 14), slice(242, 256)):
            for xs in (slice(0, 14), slice(242, 256)):
                corners.append(float(half[ys, xs].mean()))
    centres = [float(g[100:156, 100:156].mean()), float(g[100:156, 356:412].mean())]
    return max(corners) < 12 and min(centres) > 20


def probe(path, allow_download=False):
    """What a file is. kind: 360 | not-360 | fisheye | unsupported | camera-raw | online-only | slideshow | unreadable"""
    path = os.path.abspath(path)
    info = {"path": path, "name": os.path.basename(path), "size": None, "mtime": None, "online_only": False,
            "kind": None, "reason": "", "width": None, "height": None, "fps": None, "duration": None,
            "vcodec": None, "pix_fmt": None, "bitrate": None, "key_spacing": None, "audio": None,
            "projection": None, "stereo": "mono", "tagged": False, "creation_time": None}
    try:
        st = os.stat(path)
    except OSError as e:
        info.update(kind="unreadable", reason=str(e))
        return info
    info["size"], info["mtime"] = st.st_size, st.st_mtime
    ext = os.path.splitext(path)[1].lower()
    if ext in CAMERA_RAW_EXT:
        info.update(kind="camera-raw", reason="camera original: export it as a 360 (equirectangular) .mp4 "
                                              "from the camera's app first")
        return info
    info["online_only"] = online_only(path)
    if info["online_only"] and not allow_download:
        info.update(kind="online-only", reason="OneDrive online-only file: not opened, so nothing was downloaded")
        return info
    try:
        c = av.open(path)
    except Exception as e:  # noqa: BLE001 - any FFmpeg error means we can't use the file
        info.update(kind="unreadable", reason="can't open: %s" % e)
        return info
    try:
        if not c.streams.video:
            info.update(kind="not-360", reason="no video in the file")
            return info
        vs = c.streams.video[0]
        cc = vs.codec_context
        info.update(width=cc.width, height=cc.height, vcodec=cc.name,
                    pix_fmt=cc.format.name if cc.format else None, bitrate=vs.bit_rate or c.bit_rate or None)
        rate = vs.average_rate or vs.guessed_rate
        info["fps"] = float(rate) if rate else None
        dur = float(vs.duration * vs.time_base) if vs.duration else None
        info["duration"] = dur or (c.duration / 1e6 if c.duration else None)
        if c.streams.audio:
            a = c.streams.audio[0]
            info["audio"] = {"codec": a.codec_context.name, "channels": _channels(a.codec_context.layout),
                             "rate": a.codec_context.sample_rate, "bitrate": a.bit_rate}
        info["creation_time"] = c.metadata.get("creation_time")
        comment = c.metadata.get("comment", "") or ""
        if ext in VIDEO_EXT:
            sph = mp4meta.read_spherical(path)
            ki = mp4meta.keyframe_info(path)
            if ki:
                info["key_spacing"] = ki["spacing"]
            info.update(projection=sph["projection"], stereo=sph["stereo"], tagged=bool(sph["projection"]))
        if comment.startswith(paths.APP_NAME):
            info.update(kind="slideshow", reason="already made by %s" % paths.APP_NAME)
            return info
        w, h = info["width"], info["height"]
        if info["projection"] == "equirectangular":
            info.update(kind="360", reason="360 tag (equirectangular%s)" % (
                "" if info["stereo"] == "mono" else ", " + info["stereo"]))
        elif info["projection"]:
            info.update(kind="unsupported", reason="%s projection: only equirectangular 360 is supported"
                        % info["projection"])
        elif w and h and w >= 1920 and abs(w - 2 * h) <= max(8, w // 200):
            if _looks_dual_fisheye(c, vs):
                info.update(kind="fisheye", reason="two unstitched fisheye circles: export it as 360 "
                                                   "from the camera's app first")
            else:
                info.update(kind="360", reason="2:1 frame without a 360 tag (the output gets one)")
        else:
            info.update(kind="not-360", reason="%dx%d is not a 360 (2:1) frame and has no 360 tag" % (w, h))
        return info
    finally:
        c.close()


# ------------------------------------------------------------------ naming and sizes

def out_size(p, w, h):
    width = w if p["width"] == "original" or p["width"] >= w else p["width"]
    width -= width % 2
    height = int(round(width * h / w / 2.0)) * 2
    return width, height


def output_path(src, p, w, h, out_dir=None):
    width, _ = out_size(p, w, h)
    stem = os.path.splitext(os.path.basename(src))[0]
    label = "%ss %dpx" % (presetlib.fmt_num(p["interval"]), width)
    folder = out_dir or os.path.dirname(os.path.abspath(src))
    if p["output"] == "photos":
        return os.path.join(folder, "%s (360 frames %s)" % (stem, label))
    if p["output"] == "chapters":
        return os.path.join(folder, "%s (360 chapters %s %dfps).mp4" % (stem, label, CHAPTER_FPS))
    label += " " + CODEC_LABELS[p["codec"]]
    if p["timing"] == "hold":
        label += ", %ss each" % presetlib.fmt_num(p["hold"])
    return os.path.join(folder, "%s (360 slideshow %s).mp4" % (stem, label))


def size_of(path):
    if os.path.isdir(path):
        return sum(os.path.getsize(os.path.join(path, f)) for f in os.listdir(path))
    return os.path.getsize(path) if os.path.exists(path) else 0


def human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%.0f %s" if unit in ("B", "KB") or n >= 100 else "%.1f %s") % (n, unit)
        n /= 1024.0
    return "%.1f TB" % n


# ------------------------------------------------------------------ choosing the panoramas

def _gray_small(frame):
    h = max(2, int(round(SCORE_W * frame.height / frame.width / 2.0)) * 2)
    return frame.reformat(width=SCORE_W, height=h, format="gray").to_ndarray().astype(np.float32)


def sharpness(gray):
    """Variance of the Laplacian over the middle 60 % of the height (zenith and nadir are stretched)."""
    band = gray[gray.shape[0] // 5: gray.shape[0] * 4 // 5]
    lap = (band[:-2, 1:-1] + band[2:, 1:-1] + band[1:-1, :-2] + band[1:-1, 2:]) - 4.0 * band[1:-1, 1:-1]
    return float(lap.var())


def _signature(gray):
    h4, w4 = gray.shape[0] // 4 * 4, gray.shape[1] // 4 * 4
    return gray[:h4, :w4].reshape(h4 // 4, 4, w4 // 4, 4).mean(axis=(1, 3))


class Pick:
    __slots__ = ("window", "t", "frame", "score")

    def __init__(self, window, t, frame, score):
        self.window, self.t, self.frame, self.score = window, t, frame, score


class Picker:
    """One panorama per interval window: the sharpest candidate (or the one nearest the window start)."""

    def __init__(self, p):
        self.interval = p["interval"]
        self.nearest = p["pick"] == "nearest"
        self.skip_unchanged = p["skip_unchanged"]
        self.cur = None
        self.best = None            # (Pick, gray)
        self.last_sig = None
        self.skipped = 0

    def offer(self, t, frame):
        win = int(t // self.interval)
        out = []
        if self.cur is not None and win != self.cur:
            out = self._close()
        self.cur = win
        gray = _gray_small(frame)
        score = -abs(t - win * self.interval) if self.nearest else sharpness(gray)
        if self.best is None or score > self.best[0].score:
            self.best = (Pick(win, t, frame, score), gray)
        return out

    def finish(self):
        return self._close()

    def _close(self):
        if self.best is None:
            return []
        pick, gray = self.best
        self.best = None
        if self.skip_unchanged:
            sig = _signature(gray)
            if self.last_sig is not None and float(np.abs(sig - self.last_sig).mean()) < UNCHANGED_MAD:
                self.skipped += 1
                return []
            self.last_sig = sig
        return [pick]


def _limit_decoder(stream):
    cc = stream.codec_context
    cc.thread_type = "AUTO"
    cc.thread_count = DECODE_THREADS


def use_keyframes(p, info):
    if p["search"] == "keyframes":
        return True
    if p["search"] == "every":
        return False
    sp = info.get("key_spacing")
    return bool(sp) and sp <= p["interval"] / 1.9          # at least ~2 keyframes in every window


def _picks(ic, p, info, progress, cancel, on_audio=None):
    """Read the video once and yield the chosen panoramas in order. Audio packets go to on_audio."""
    vs = ic.streams.video[0]
    _limit_decoder(vs)
    keyonly = use_keyframes(p, info)
    ast = ic.streams.audio[0] if (on_audio and ic.streams.audio) else None
    t0 = float(vs.start_time * vs.time_base) if vs.start_time is not None else 0.0
    dur = info.get("duration") or 0.0
    picker = Picker(p)
    next_t = 0.0
    last_report = 0.0
    shown = 0
    for pkt in ic.demux(*([vs, ast] if ast else [vs])):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        if ast is not None and pkt.stream is ast:
            on_audio(pkt)
            continue
        if pkt.pts is None or (keyonly and not pkt.is_keyframe):
            continue
        for fr in pkt.decode():
            if fr.pts is None:
                continue
            t = float(fr.pts * fr.time_base) - t0
            if not keyonly:
                if t + 1e-6 < next_t:
                    continue
                next_t = (int(t / SAMPLE_STEP) + 1) * SAMPLE_STEP
            for pick in picker.offer(max(t, 0.0), fr):
                shown += 1
                yield pick
            now = time.monotonic()
            if progress and now - last_report > 0.25:
                last_report = now
                progress(min(t / dur, 1.0) if dur else 0.0,
                         "%s of %s read, %d panoramas" % (_mmss(t), _mmss(dur), shown))
    for fr in vs.codec_context.decode(None):
        if fr.pts is not None:
            t = float(fr.pts * fr.time_base) - t0
            for pick in picker.offer(max(t, 0.0), fr):
                yield pick
    for pick in picker.finish():
        yield pick
    info["_skipped_repeats"] = picker.skipped
    info["_keyframes_only"] = keyonly


def _mmss(t):
    t = int(t or 0)
    return "%d:%02d:%02d" % (t // 3600, t % 3600 // 60, t % 60) if t >= 3600 else "%d:%02d" % (t // 60, t % 60)


def _clock(t, tenths=False):
    """Time in the original video as a chapter title: 1:37 (or 1:37.5 when panoramas are < 1 s apart)."""
    return _mmss(t) + (".%d" % (int(round((t or 0) * 10)) % 10) if tenths else "")


def _short(e):
    return ("%s: %s" % (type(e).__name__, e))[:160]


# ------------------------------------------------------------------ GPU: what works here (tested once per session)

def _gpu_pix(name):
    return "nv12" if name.endswith("_qsv") else "yuv420p"


def _gpu_gop(name, gop):
    return 0 if gop == 1 and name.endswith("_nvenc") else gop      # NVENC: 0 = every frame a keyframe (1 fails)


def _gpu_options(name, qp):
    if name.endswith("_nvenc"):
        return {"preset": "p4", "tune": "hq", "rc": "constqp", "qp": str(qp),
                "profile": "main" if name.startswith("hevc") else "high"}
    return {"preset": "veryslow", "flags": "+qscale", "global_quality": str(qp * QSV_LAMBDA),   # Quick Sync, constant QP
            "i_qfactor": "1", "i_qoffset": "0"}


def _lin(points, x):
    """Linear interpolation in {x: y}, held flat beyond the ends."""
    xs = sorted(points)
    if x <= xs[0]:
        return points[xs[0]]
    if x >= xs[-1]:
        return points[xs[-1]]
    a = max(v for v in xs if v <= x)
    b = min(v for v in xs if v >= x)
    return points[a] if a == b else points[a] + (x - a) * (points[b] - points[a]) / float(b - a)


def gpu_qp(name, crf, width):
    """The GPU quantizer that matches the CPU encoder's quality at this crf and output width
    (offsets interpolated over crf, and over the width on a log scale)."""
    table = GPU_QP_OFFSET[name]
    ws = sorted(table)
    if width <= ws[0]:
        off = _lin(table[ws[0]], crf)
    elif width >= ws[-1]:
        off = _lin(table[ws[-1]], crf)
    else:
        a = max(w for w in ws if w <= width)
        b = min(w for w in ws if w >= width)
        f = 0.0 if a == b else (math.log(width) - math.log(a)) / (math.log(b) - math.log(a))
        off = _lin(table[a], crf) + f * (_lin(table[b], crf) - _lin(table[a], crf))
    return max(1, min(51, int(round(crf + off))))


def gpu_available(name, width, height):
    """-> (works, reason): can this GPU encoder make a frame of this size here? Tested once per session."""
    key = (name, int(width), int(height))
    if key not in _gpu_state:
        _gpu_state[key] = _try_gpu(name, int(width), int(height))
    return _gpu_state[key]


def _try_gpu(name, width, height):
    if NO_GPU:
        return False, "GPU switched off (V360_NO_GPU=1)"
    try:
        cc = av.CodecContext.create(name, "w")
        cc.width, cc.height, cc.pix_fmt = width, height, _gpu_pix(name)
        cc.time_base, cc.framerate = Fraction(1, 1000), Fraction(1, 5)
        cc.gop_size, cc.max_b_frames = _gpu_gop(name, 1), 0
        cc.options = _gpu_options(name, 30)
        cc.open()
        f = av.VideoFrame(width, height, _gpu_pix(name))
        f.pts = 0
        if not (cc.encode(f) + cc.encode(None)):
            return False, "the encoder gave no output"
        return True, ""
    except Exception as e:  # noqa: BLE001 - no GPU, no driver, size too big ...
        return False, _short(e)


def encoder_plan(p, width=None, height=None):
    """-> (encoders to try in order, the CPU last; notes). GPU encoders are listed untested: each is tested
    (once per session and size) just before its turn, so a second GPU costs nothing unless the first fails."""
    choice = p.get("encoder", "auto")
    order = AUTO_ORDER.get(p["codec"], ()) if choice == "auto" else ENCODER_ORDER.get(choice, ())
    plan, notes = [], []
    for hw in order:
        name = GPU_ENCODERS.get((p["codec"], hw))
        if name:
            plan.append(name)
        elif choice == hw:
            notes.append("the %s has no %s encoder, so it was made on the CPU" % (HW_NAMES[hw],
                                                                                  CODEC_LABELS[p["codec"]]))
    return plan + [CODECS[p["codec"]]], notes


def _cant_note(name, p, width, height):
    hw = HW_NAMES.get("nvidia" if name.endswith("_nvenc") else "intel")
    limit = " (its H.264 limit is 4096 px wide)" if p["codec"] == "h264" and width > 4096 else ""
    return "the %s can't encode %s at %dx%d%s" % (hw, CODEC_LABELS[p["codec"]], width, height, limit)


def _hwdec_sample():
    """A tiny HEVC clip for testing the hardware decoders (made once, in the work folder)."""
    path = os.path.join(paths.WORK_DIR, "hwdec-test-640x320.mp4")
    if os.path.exists(path):
        return path
    paths.ensure_dirs()
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with av.open(tmp, "w", format="mp4") as oc:
        st = oc.add_stream("libx265", rate=5, options={"preset": "ultrafast", "crf": "30",
                                                        "x265-params": "log-level=error"})
        st.width, st.height, st.pix_fmt = 640, 320, "yuv420p"
        for i in range(3):
            f = av.VideoFrame.from_ndarray(np.full((320, 640, 3), 60 * i, np.uint8), format="rgb24")
            f = f.reformat(format="yuv420p")
            f.pts = i
            for pkt in st.encode(f):
                oc.mux(pkt)
        for pkt in st.encode(None):
            oc.mux(pkt)
    os.replace(tmp, path)
    return path


def hwdec_available(dev):
    """Does this hardware decoder (cuda / d3d11va) decode HEVC here? Tested once per session."""
    if dev not in _hwdec_state:
        ok = False
        if not NO_GPU:
            try:
                from av.codec.hwaccel import HWAccel
                with av.open(_hwdec_sample(), hwaccel=HWAccel(device_type=dev, allow_software_fallback=False)) as c:
                    vs = c.streams.video[0]
                    for _ in c.decode(vs):
                        ok = bool(getattr(vs.codec_context, "is_hwaccel", False))
                        break
            except Exception:  # noqa: BLE001
                ok = False
        _hwdec_state[dev] = ok
    return _hwdec_state[dev]


def _open_source(src, info, hwdec):
    """Open the video for reading -> (container, device). HEVC is decoded on the GPU when hwdec and one can."""
    if hwdec and not NO_GPU and info.get("vcodec") in HWDEC_CODECS:
        for dev in HWDEC_DEVICES:
            if hwdec_available(dev):
                from av.codec.hwaccel import HWAccel
                return av.open(src, hwaccel=HWAccel(device_type=dev, allow_software_fallback=True)), dev
    return av.open(src), "software"


def _decoder_used(ic, dev):
    try:
        return dev if dev != "software" and ic.streams.video[0].codec_context.is_hwaccel else "software"
    except AttributeError:
        return "software"


def gpu_report():
    """What the GPUs here can do (for 'shrink360.py gpu' and the window's Tools menu)."""
    rows = []
    for (codec, hw), name in GPU_ENCODERS.items():
        best, why = None, ""
        for w, h in ((7680, 3840), (5760, 2880), (3840, 1920), (1920, 960)):
            ok, reason = gpu_available(name, w, h)
            if ok:
                best = "%dx%d" % (w, h)
                break
            why = why or reason
        rows.append({"codec": CODEC_LABELS[codec], "gpu": HW_NAMES[hw], "encoder": name, "largest_tested": best,
                     "reason": "" if best else why})
    return {"encoders": rows, "hevc_decoders": {d: hwdec_available(d) for d in HWDEC_DEVICES}, "no_gpu": NO_GPU}


# ------------------------------------------------------------------ writing a slideshow video

def _video_stream(oc, p, width, height, encoder=None):
    name = encoder or CODECS[p["codec"]]
    gop = max(1, int(p["keyframe_every"]))
    look = 0 if gop == 1 else min(gop, 4)
    pix = "yuv420p"
    if name in GPU_ENCODER_NAMES:
        opts, pix = _gpu_options(name, gpu_qp(name, p["crf"], width)), _gpu_pix(name)
    else:
        opts = {"crf": str(p["crf"]), "preset": SPEEDS[name][p["speed"]]}
        n = ENCODE_THREADS
        if name == "libx264":
            opts["x264-params"] = ("keyint=%d:min-keyint=1:scenecut=0:bframes=0:rc-lookahead=%d:threads=%d"
                                   % (gop, look, n))
        elif name == "libx265":
            # keyint=1 makes x265 label the stream HEVC "Main Intra" (a RExt profile) that hardware decoders
            # refuse; keyint=2 with every frame forced to IDR (_VideoWriter) gives the same frames as Main
            opts["x265-params"] = ("keyint=%d:min-keyint=1:scenecut=0:bframes=0:rc-lookahead=%d:frame-threads=%d"
                                   ":pools=%d:log-level=error" % (max(gop, 2), look, max(1, n // 2), max(2, n * 2)))
            if gop == 1:
                opts["forced-idr"] = "1"
        else:
            opts["svtav1-params"] = "keyint=%d:scd=0:lp=%d" % (gop, max(1, n // 2))
    st = oc.add_stream(name, options=opts)
    st.width, st.height, st.pix_fmt = width, height, pix
    st.time_base = Fraction(1, 1000)
    cc = st.codec_context
    cc.time_base = Fraction(1, 1000)
    step = p["interval"] if p["timing"] == "real" else p["hold"]
    cc.framerate = Fraction(1000, max(1, int(round(step * 1000))))
    cc.gop_size = _gpu_gop(name, gop) if name in GPU_ENCODER_NAMES else (max(gop, 2) if name == "libx265" else gop)
    cc.max_b_frames = 0
    if p["codec"] == "hevc":
        cc.codec_tag = "hvc1"                       # the tag Apple and Insta360 use for HEVC in MP4
    return st, pix


def _chapters(starts_ms, times, end_ms, p):
    """One chapter per panorama, titled with its time in the original video (VLC: Shift+N / Shift+P)."""
    tenths = p["interval"] < 1
    out = []
    for i, ms in enumerate(starts_ms):
        end = starts_ms[i + 1] if i + 1 < len(starts_ms) else max(end_ms, ms + 1)
        out.append({"id": i + 1, "start": ms, "end": end, "time_base": Fraction(1, 1000),
                    "metadata": {"title": _clock(times[i], tenths)}})
    return out


class _VideoWriter:
    """Encodes the picks with millisecond timestamps; each frame lasts until the next one starts."""

    def __init__(self, oc, st, p, width, height, src_width, pix="yuv420p"):
        self.oc, self.st, self.p = oc, st, p
        self.width, self.height, self.pix = width, height, pix
        self.interp = "AREA" if width < src_width else None
        # x265 all-intra: every frame is asked for as an IDR keyframe (see _video_stream)
        self.force_key = st.codec_context.name == "libx265" and int(p["keyframe_every"]) == 1
        self.fed = []
        self.times = []                             # each panorama's time in the original (chapter titles)
        self.pending = collections.deque()
        self.count = 0

    def add(self, pick):
        if self.p["timing"] == "real":
            ms = 0 if not self.fed else int(round(pick.window * self.p["interval"] * 1000))
        else:
            ms = int(round(self.count * self.p["hold"] * 1000))
        if self.fed and ms <= self.fed[-1]:
            ms = self.fed[-1] + 1
        kw = {"width": self.width, "height": self.height, "format": self.pix}
        if self.interp:
            kw["interpolation"] = self.interp
        f = pick.frame.reformat(**kw)
        f.pts = ms
        f.time_base = Fraction(1, 1000)
        if self.force_key:
            f.pict_type = PictureType.I
        self.fed.append(ms)
        self.times.append(pick.t)
        self.count += 1
        for pkt in self.st.encode(f):
            self.pending.append(pkt)
        self._flush()

    def _flush(self, end_ms=None):
        while self.pending:
            pkt = self.pending[0]
            i = bisect.bisect_right(self.fed, pkt.pts)
            if i < len(self.fed):
                pkt.duration = self.fed[i] - pkt.pts
            elif end_ms is not None:
                pkt.duration = max(1, end_ms - pkt.pts)
            else:
                break
            self.oc.mux(pkt)
            self.pending.popleft()

    def close(self, end_ms):
        for pkt in self.st.encode(None):
            self.pending.append(pkt)
        self._flush(end_ms)


class _AudioOut:
    """Sound for real-time slideshows: copied unchanged, or re-encoded to AAC (speech mono / standard stereo)."""

    def __init__(self, oc, ist, mode, t0):
        self.oc, self.ist, self.mode = oc, ist, mode
        self.off = int(round(t0 / ist.time_base)) if ist.time_base else 0
        if mode == "copy":
            self.ost = oc.add_stream_from_template(ist)
            return
        layout = "mono" if mode == "speech" else "stereo"
        self.ost = oc.add_stream("aac", rate=48000)
        cc = self.ost.codec_context
        cc.layout = layout
        cc.format = "fltp"
        cc.bit_rate = 48000 if mode == "speech" else 96000
        self.res = av.AudioResampler(format="fltp", layout=layout, rate=48000)
        self.fifo = av.AudioFifo()
        self.samples = 0

    def packet(self, pkt):
        if self.mode == "copy":
            if pkt.pts is None or pkt.size == 0:
                return
            pkt.pts -= self.off
            if pkt.dts is not None:
                pkt.dts -= self.off
            if pkt.pts < 0:
                return
            pkt.stream = self.ost
            self.oc.mux(pkt)
            return
        for fr in pkt.decode():
            for r in self.res.resample(fr):
                self._push(r)

    def _push(self, fr, final=False):
        if fr is not None:
            fr.pts = None
            self.fifo.write(fr)
        while self.fifo.samples >= 1024 or (final and self.fifo.samples):
            out = self.fifo.read(min(1024, self.fifo.samples))
            out.pts = self.samples
            out.time_base = Fraction(1, 48000)
            self.samples += out.samples
            for p in self.ost.encode(out):
                self.oc.mux(p)

    def close(self):
        if self.mode == "copy":
            return
        for r in self.res.resample(None):
            self._push(r)
        self._push(None, final=True)
        for p in self.ost.encode(None):
            self.oc.mux(p)


def _make_video(src, info, p, width, height, out, progress, cancel, encoder=None, hwdec=False):
    """-> (panoramas written, decoder used)."""
    encoder = encoder or CODECS[p["codec"]]
    ic, dev = _open_source(src, info, hwdec)
    try:
        real = p["timing"] == "real"
        want_audio = real and p["audio"] != "none" and bool(ic.streams.audio)
        # chapters: only FFmpeg's QuickTime chapter track (its Nero 'chpl' box stops VLC at 255 chapters)
        oc = av.open(out, "w", format="mp4", options={"movflags": "+disable_chpl"})
        try:
            vst, pix = _video_stream(oc, p, width, height, encoder)
            audio = None
            if want_audio:
                vs = ic.streams.video[0]
                t0 = float(vs.start_time * vs.time_base) if vs.start_time is not None else 0.0
                audio = _AudioOut(oc, ic.streams.audio[0], p["audio"], t0)
            oc.metadata["title"] = os.path.splitext(os.path.basename(src))[0]
            oc.metadata["comment"] = "%s %s: %s; encoder %s. From %s" % (
                paths.APP_NAME, paths.VERSION, presetlib.summary(p), encoder, os.path.basename(src))
            if info.get("creation_time"):
                oc.metadata["creation_time"] = info["creation_time"]
            writer = _VideoWriter(oc, vst, p, width, height, info["width"], pix)
            for pick in _picks(ic, p, info, progress, cancel, audio.packet if audio else None):
                writer.add(pick)
                pick.frame = None
            if not writer.fed:
                raise RuntimeError("no frames could be read from the video")
            if real:                                 # the last panorama stays up until the original ends
                dur_ms = int(round((info.get("duration") or 0) * 1000))
                end_ms = dur_ms if dur_ms > writer.fed[-1] else writer.fed[-1] + int(round(p["interval"] * 1000))
            else:
                end_ms = writer.fed[-1] + int(round(p["hold"] * 1000))
            writer.close(end_ms)
            if audio:
                audio.close()
            oc.set_chapters(_chapters(writer.fed, writer.times, end_ms, p))   # written with the trailer
            frames = writer.count
        finally:
            oc.close()
        decoder = _decoder_used(ic, dev)
    finally:
        ic.close()
    mp4meta.inject_spherical(out, stereo=info.get("stereo") or "mono", software=paths.APP_NAME)
    return frames, decoder


# ------------------------------------------------------------------ writing a chaptered video (1.2)
# The whole video stays playable, at 12 frames a second, and in every interval the sharpest frame (any frame,
# not only the source's keyframes) becomes an IDR keyframe with a chapter on it, so VLC's Shift+N / Shift+P
# land on it. Every frame has the same quantizer (video_qp): with softer frames between the chapter frames
# the whole video looked soft while it played. One reading pass: each interval's frames are held in memory
# until it ends (about 1 GB per 2 s at 8K).
# Measured on a 43 s 8K X6 export (652 MB), NVENC p7: qp 22 = 197 MB in 68 s, every frame 43-44 dB PSNR and
# edge width equal to the original's; qp 26 = 58 % of that size, about 1.5 dB lower.

CHAPTER_FPS = 12
CHAPTER_MAX_INTERVAL = 5.0                       # memory: the frames of one interval are held until it ends
CHAPTER_SPEEDS = {"hevc_qsv": {"fast": "veryfast", "medium": "medium", "slow": "veryslow"},
                  "hevc_nvenc": {"fast": "p1", "medium": "p7", "slow": "p7"},
                  "libx265": {"fast": "fast", "medium": "medium", "slow": "slow"}}


def _chapter_stream(oc, p, width, height, name, tb):
    key = mid = int(p["video_qp"])
    speed = CHAPTER_SPEEDS[name][p["speed"]]
    if name == "hevc_qsv":
        pix = "nv12"
        opts = {"preset": speed, "flags": "+qscale", "global_quality": str(mid * QSV_LAMBDA),
                "i_qfactor": "1", "i_qoffset": str(key - mid), "forced_idr": "1"}
    elif name == "hevc_nvenc":
        pix = "nv12"
        opts = {"preset": speed, "tune": "hq", "rc": "constqp", "init_qpI": str(key), "init_qpP": str(mid),
                "init_qpB": str(mid), "forced-idr": "1", "profile": "main"}
    else:
        # x265 at constant QP: ipratio 1 gives the I frames (the chapter frames) the same qp as the others
        pix = "yuv420p"
        n = ENCODE_THREADS
        opts = {"preset": speed, "forced-idr": "1",
                "x265-params": "keyint=100000:min-keyint=1:scenecut=0:bframes=0:qp=%d:ipratio=%.4f:rc-lookahead=4"
                               ":frame-threads=%d:pools=%d:log-level=error"
                               % (mid, 2 ** ((mid - key) / 6.0), max(1, n // 2), max(2, n * 2))}
    st = oc.add_stream(name, options=opts)
    st.width, st.height, st.pix_fmt = width, height, pix
    st.time_base = tb
    cc = st.codec_context
    cc.time_base = tb
    cc.framerate = Fraction(CHAPTER_FPS)
    cc.gop_size = 100000 if name == "libx265" else 1000
    cc.max_b_frames = 0
    cc.codec_tag = "hvc1"
    return st, pix


def _make_chapters(src, info, p, width, height, out, progress, cancel, encoder, hwdec=False):
    """-> (frames written, decoder used). Sets info['_chapters'] (the number of chapters)."""
    ic, dev = _open_source(src, info, hwdec)
    try:
        vs = ic.streams.video[0]
        cc_in = vs.codec_context
        cc_in.thread_type = "AUTO"
        cc_in.thread_count = max(DECODE_THREADS, 8)           # every frame is decoded here
        tb = vs.time_base
        start = vs.start_time or 0
        t0 = float(start * tb)
        dur = info.get("duration") or 0.0
        step = max(1, int(round((info.get("fps") or 24.0) / CHAPTER_FPS)))
        interval = p["interval"]
        oc = av.open(out, "w", format="mp4", options={"movflags": "+disable_chpl"})
        try:
            vst, pix = _chapter_stream(oc, p, width, height, encoder, tb)
            audio = None
            if p["audio"] != "none" and ic.streams.audio:
                audio = _AudioOut(oc, ic.streams.audio[0], p["audio"], t0)
            oc.metadata["title"] = os.path.splitext(os.path.basename(src))[0]
            oc.metadata["comment"] = "%s %s: %s; encoder %s. From %s" % (
                paths.APP_NAME, paths.VERSION, presetlib.summary(p), encoder, os.path.basename(src))
            if info.get("creation_time"):
                oc.metadata["creation_time"] = info["creation_time"]
            kw = {"format": pix}
            if width != info["width"]:
                kw.update(width=width, height=height, interpolation="AREA")
            state = {"frames": 0, "last_pts": None}
            chap_t = []
            buf = []                                  # [frame, score, kept] of the current interval
            win = {"cur": None, "best": -1.0}

            def write(fr, chapter):
                ef = fr.reformat(**kw)
                ef.pts = fr.pts - start
                if state["last_pts"] is not None and ef.pts <= state["last_pts"]:
                    ef.pts = state["last_pts"] + 1
                state["last_pts"] = ef.pts
                ef.time_base = tb
                ef.pict_type = PictureType.I if chapter else PictureType.NONE
                for pkt in vst.encode(ef):
                    oc.mux(pkt)
                state["frames"] += 1

            def flush():
                if not buf:
                    return
                bi = max(range(len(buf)), key=lambda i: buf[i][1])
                chap_t.append(max(0.0, float((buf[bi][0].pts - start) * tb)))
                for i, (fr, _s, kept) in enumerate(buf):
                    if i == bi or kept:
                        write(fr, i == bi)
                buf.clear()
                win["best"] = -1.0

            n = 0
            last_report = 0.0
            streams = [vs] + ([ic.streams.audio[0]] if audio else [])
            for pkt in ic.demux(*streams):
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                if audio is not None and pkt.stream is not vs:
                    audio.packet(pkt)
                    continue
                for fr in pkt.decode():
                    if fr.pts is None:
                        continue
                    t = float((fr.pts - start) * tb)
                    w = int(max(t, 0.0) // interval)
                    if win["cur"] is not None and w != win["cur"]:
                        flush()
                    win["cur"] = w
                    kept = n % step == 0
                    s = sharpness(_gray_small(fr))
                    if kept or s > win["best"]:
                        if not kept:                  # an earlier frame that is neither kept nor the best can go
                            buf[:] = [e for e in buf if e[2]]
                        buf.append([fr, s, kept])
                    win["best"] = max(win["best"], s)
                    n += 1
                    now = time.monotonic()
                    if progress and now - last_report > 0.25:
                        last_report = now
                        progress(min(t / dur, 1.0) if dur else 0.0,
                                 "%s of %s read, %d chapters" % (_mmss(t), _mmss(dur), len(chap_t)))
            flush()                                   # (demux's last, empty packet already drained the decoder)
            if not state["frames"]:
                raise RuntimeError("no frames could be read from the video")
            for pkt in vst.encode(None):
                oc.mux(pkt)
            if audio:
                audio.close()
            end_ms = max(int(round(dur * 1000)), int(round(chap_t[-1] * 1000)) + 1)
            starts = [int(round(x * 1000)) for x in chap_t]
            for i in range(1, len(starts)):               # chapter starts must increase
                starts[i] = max(starts[i], starts[i - 1] + 1)
            titles = [_clock(x, True) for x in chap_t]
            if starts[0] > 0:
                # an MP4 chapter track always starts its first chapter at 0, which would move the first sharp
                # frame's chapter to the start: give the start a chapter of its own
                starts, titles = [0] + starts, ["Start"] + titles
            oc.set_chapters([{"id": i + 1, "start": ms, "end": starts[i + 1] if i + 1 < len(starts) else
                              max(end_ms, ms + 1), "time_base": Fraction(1, 1000),
                              "metadata": {"title": titles[i]}} for i, ms in enumerate(starts)])
            info["_chapters"] = len(starts)
            info["_sharp_frames"] = len(chap_t)
            frames = state["frames"]
        finally:
            oc.close()
        decoder = _decoder_used(ic, dev)
    finally:
        ic.close()
    mp4meta.inject_spherical(out, stereo=info.get("stereo") or "mono", software=paths.APP_NAME)
    return frames, decoder


# ------------------------------------------------------------------ writing JPEG panoramas

def _exif(info, t, src):
    exif = Image.Exif()
    exif[0x010E] = "360 panorama at %s in %s" % (_mmss(t), os.path.basename(src))   # ImageDescription
    exif[0x0131] = "%s %s" % (paths.APP_NAME, paths.VERSION)                        # Software
    ct = info.get("creation_time")
    if ct:
        try:
            when = datetime.datetime.fromisoformat(ct.replace("Z", "+00:00"))
            if when.tzinfo:
                when = when.astimezone()
            when += datetime.timedelta(seconds=t)
            exif.get_ifd(0x8769)[0x9003] = when.strftime("%Y:%m:%d %H:%M:%S")    # DateTimeOriginal
        except ValueError:
            pass
    return exif


def _make_photos(src, info, p, width, height, folder, progress, cancel, hwdec=False):
    """-> (panoramas written, decoder used)."""
    os.makedirs(folder)
    xmp = GPANO_XMP.format(w=width, h=height).encode("utf-8")
    interp = "AREA" if width < info["width"] else None
    ic, dev = _open_source(src, info, hwdec)
    n = 0
    decoder = "software"
    try:
        for pick in _picks(ic, p, info, progress, cancel):
            kw = {"width": width, "height": height, "format": "rgb24"}
            if interp:
                kw["interpolation"] = interp
            img = pick.frame.reformat(**kw).to_image()
            pick.frame = None
            name = "%04ds 360.jpg" % int(pick.t)
            k = 2
            while os.path.exists(os.path.join(folder, name)):
                name = "%04ds %d 360.jpg" % (int(pick.t), k)
                k += 1
            img.save(os.path.join(folder, name), "JPEG", quality=p["jpeg_quality"], optimize=True,
                     xmp=xmp, exif=_exif(info, pick.t, src))
            n += 1
        decoder = _decoder_used(ic, dev)
    finally:
        ic.close()
    return n, decoder


# ------------------------------------------------------------------ checking the result

def verify_output(path, p, frames, info):
    """Decode every frame back; check the count, the length, the 360 tags, the keyframes and the chapters
    (or the JPEGs)."""
    problems = []
    if p["output"] == "photos":
        files = sorted(f for f in os.listdir(path) if f.lower().endswith(".jpg"))
        if len(files) != frames:
            problems.append("%d JPEGs, expected %d" % (len(files), frames))
        for f in files:
            with Image.open(os.path.join(path, f)) as im:
                im.verify()
        if files:
            with open(os.path.join(path, files[0]), "rb") as fh:
                if b"GPano:ProjectionType" not in fh.read(256 * 1024):
                    problems.append("360 photo tag missing")
        return {"ok": not problems, "problem": "; ".join(problems), "frames": len(files)}
    if p["output"] == "chapters":
        return _verify_chapters(path, p, frames, info)
    c = av.open(path)
    try:
        vs = c.streams.video[0]
        _limit_decoder(vs)
        n = sum(1 for _ in c.decode(vs))
        dur = c.duration / 1e6 if c.duration else 0.0
        has_audio = bool(c.streams.audio)
        chapters = len(c.chapters())
    finally:
        c.close()
    if n != frames:
        problems.append("%d frames decode, expected %d" % (n, frames))
    if chapters != frames:
        problems.append("%d chapters, expected one per panorama (%d)" % (chapters, frames))
    ki = mp4meta.keyframe_info(path)
    gop = max(1, int(p["keyframe_every"]))
    need = (frames + gop - 1) // gop
    if ki and ki["keyframes"] < need:                 # every panorama (or every Nth) must be a seek point
        problems.append("%d keyframes, expected %d" % (ki["keyframes"], need))
    sph = mp4meta.read_spherical(path)
    if not (sph["v1"] and sph["v2"] and sph["projection"] == "equirectangular"):
        problems.append("360 tag missing")
    src_dur = info.get("duration") or 0.0
    if p["timing"] == "real" and src_dur and abs(dur - src_dur) > max(1.5, 0.02 * src_dur):
        problems.append("length %.1f s, original %.1f s" % (dur, src_dur))
    if p["timing"] == "real" and p["audio"] != "none" and info.get("audio") and not has_audio:
        problems.append("sound missing")
    return {"ok": not problems, "problem": "; ".join(problems), "frames": n, "duration": dur, "audio": has_audio,
            "chapters": chapters, "keyframes": ki["keyframes"] if ki else None}


def _verify_chapters(path, p, frames, info):
    """Chaptered video: count the frames (packets, not decoded: 8K is slow to decode), decode every keyframe,
    and check that every chapter starts on a keyframe, plus the length, the sound and the 360 tags."""
    problems = []
    c = av.open(path)
    try:
        vs = c.streams.video[0]
        _limit_decoder(vs)
        n, keys, decoded = 0, [], 0
        for pkt in c.demux(vs):
            if pkt.pts is None:
                continue
            n += 1
            if pkt.is_keyframe:
                keys.append(int(round(float(pkt.pts * vs.time_base) * 1000)))
                decoded += len(pkt.decode())
        decoded += len(vs.codec_context.decode(None))
        if decoded < len(keys):
            problems.append("only %d of %d keyframes decode" % (decoded, len(keys)))
        dur = c.duration / 1e6 if c.duration else 0.0
        has_audio = bool(c.streams.audio)
        chapters = [int(round(float(ch["start"] * ch["time_base"]) * 1000)) for ch in c.chapters()]
    finally:
        c.close()
    if n != frames:
        problems.append("%d frames, expected %d" % (n, frames))
    want = info.get("_chapters")
    if want is not None and len(chapters) != want:
        problems.append("%d chapters, expected %d" % (len(chapters), want))
    off = [ms for ms in chapters if not any(abs(ms - k) <= 2 for k in keys)]
    if off:
        problems.append("%d chapters don't start on a keyframe" % len(off))
    sph = mp4meta.read_spherical(path)
    if not (sph["v1"] and sph["v2"] and sph["projection"] == "equirectangular"):
        problems.append("360 tag missing")
    src_dur = info.get("duration") or 0.0
    if src_dur and abs(dur - src_dur) > max(1.0, 0.02 * src_dur):
        problems.append("length %.1f s, original %.1f s" % (dur, src_dur))
    if p["audio"] != "none" and info.get("audio") and not has_audio:
        problems.append("sound missing")
    return {"ok": not problems, "problem": "; ".join(problems), "frames": n, "duration": dur, "audio": has_audio,
            "chapters": len(chapters), "keyframes": len(keys)}


# ------------------------------------------------------------------ the whole job

def cleanup_work(max_age_hours=12):
    """Remove job folders left behind by runs that were killed (never anything else)."""
    if not os.path.isdir(paths.WORK_DIR):
        return
    limit = time.time() - max_age_hours * 3600
    for d in os.listdir(paths.WORK_DIR):
        full = os.path.join(paths.WORK_DIR, d)
        if d.startswith("job-") and os.path.isdir(full) and os.path.getmtime(full) < limit:
            shutil.rmtree(full, ignore_errors=True)


def convert(src, p, out_dir=None, progress=None, cancel=None, allow_download=False, info=None):
    """Convert one video. Never overwrites, never touches the original. -> result dict (also logged)."""
    started = time.time()
    p = presetlib.normalize(p)
    src = os.path.abspath(src)
    res = {"event": "convert", "time": datetime.datetime.now().isoformat(timespec="seconds"), "source": src,
           "preset": p["name"], "settings": p, "status": None, "message": "", "output": None, "output_size": 0,
           "source_size": None, "source_mtime": None, "frames": 0, "elapsed": 0.0, "verified": False}
    info = info if info and info.get("kind") != "online-only" else probe(src, allow_download=allow_download)
    res["source_size"], res["source_mtime"] = info.get("size"), info.get("mtime")
    res["source_info"] = {k: info.get(k) for k in ("width", "height", "fps", "duration", "vcodec", "stereo",
                                                     "key_spacing", "online_only")}
    if info["kind"] != "360":
        return _finish(res, "skipped", info["reason"] or info["kind"], started)
    width, height = out_size(p, info["width"], info["height"])
    final = output_path(src, p, info["width"], info["height"], out_dir)
    res["output"] = final
    if os.path.exists(final):
        return _finish(res, "skipped", "output already exists: %s" % os.path.basename(final), started)
    paths.ensure_dirs()
    job_dir = tempfile.mkdtemp(prefix="job-", dir=paths.WORK_DIR)
    work = os.path.join(job_dir, os.path.basename(final))
    status, message = "failed", ""
    gpu = p["encoder"] != "cpu" and not NO_GPU
    try:
        if progress:
            progress(0.0, "starting")
        # what to try, in order: the GPU encoders (each tested just before its turn), then the CPU encoder still
        # reading on the GPU, then everything on the CPU as in 1.0 (always the last try)
        if p["output"] == "photos":
            attempts, notes = ([(None, True), (None, False)] if gpu else [(None, False)]), []
        else:
            plan, notes = encoder_plan(dict(p, codec="hevc") if p["output"] == "chapters" else p, width, height)
            attempts = ([(e, True) for e in plan[:-1]] + [(plan[-1], True)]) if gpu else []
            attempts.append((plan[-1], False))
        k = 0
        while True:
            enc, hwdec = attempts[k]
            if enc in GPU_ENCODER_NAMES and not gpu_available(enc, width, height)[0]:
                if p["encoder"] != "auto":
                    notes.append(_cant_note(enc, p, width, height))
                k += 1
                continue
            _remove(work)
            try:
                if p["output"] == "photos":
                    frames, decoder = _make_photos(src, info, p, width, height, work, progress, cancel, hwdec)
                elif p["output"] == "chapters":
                    frames, decoder = _make_chapters(src, info, p, width, height, work, progress, cancel, enc, hwdec)
                else:
                    frames, decoder = _make_video(src, info, p, width, height, work, progress, cancel, enc, hwdec)
                if progress:
                    progress(1.0, "checking the result")
                check = verify_output(work, p, frames, info)
                res["check"] = check
                if not check["ok"]:
                    raise RuntimeError("check failed: " + check["problem"])
                break
            except Cancelled:
                raise
            except Exception as e:  # noqa: BLE001 - a GPU run that fails is redone; the CPU run is the last word
                if k == len(attempts) - 1:
                    raise
                what = ENCODER_LABELS[enc] if enc in GPU_ENCODER_NAMES else "GPU decoding"
                if enc in GPU_ENCODER_NAMES:
                    _gpu_state[(enc, width, height)] = (False, _short(e))   # not again in this session
                notes.append("the %s run failed (%s) and was redone" % (what, _short(e)))
                if progress:
                    progress(0.0, "the %s run failed; starting again" % what)
                k += 1
        if os.path.exists(final):
            raise RuntimeError("a file with the output name appeared meanwhile; nothing was overwritten")
        os.makedirs(os.path.dirname(final), exist_ok=True)
        shutil.move(work, final)
        mtime = info.get("mtime") or time.time()
        if os.path.isdir(final):
            for f in os.listdir(final):
                os.utime(os.path.join(final, f), (time.time(), mtime))
        os.utime(final, (time.time(), mtime))
        if enc:
            made_on = ENCODER_LABELS.get(enc, enc)
        else:
            made_on = "read on the GPU" if decoder != "software" else "CPU"
        res.update(frames=frames, verified=True, output_size=size_of(final),
                   skipped_repeats=info.get("_skipped_repeats", 0), keyframes_only=info.get("_keyframes_only"),
                   encoder=enc, decoder=decoder, made_on=made_on, notes=notes)
        status = "done"
        what = ("%d sharp-frame chapters (%d frames)" % (info.get("_sharp_frames", 0), frames)
                if p["output"] == "chapters" else "%d panoramas" % frames)
        if p["output"] == "chapters":
            res["chapters"] = info.get("_chapters", 0)
            res["sharp_frames"] = info.get("_sharp_frames", 0)
        message = "%s, %s (%s of the original), %s" % (
            what, human(res["output_size"]), ratio_text(res["output_size"], res["source_size"]), made_on)
        if notes:
            message += "; " + "; ".join(notes)
    except Cancelled:
        status, message = "cancelled", "stopped by the user"
    except Exception as e:  # noqa: BLE001 - reported to the user and the log
        status, message = "failed", "%s: %s" % (type(e).__name__, e)
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)
    return _finish(res, status, message, started)


def _remove(path):
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    elif os.path.exists(path):
        os.remove(path)


def ratio_text(out_size_, src_size):
    if not out_size_ or not src_size:
        return "?"
    r = src_size / float(out_size_)
    return "1/%d" % round(r) if r >= 2 else "%.0f%%" % (100.0 * out_size_ / src_size)


def _finish(res, status, message, started):
    res["status"], res["message"] = status, message
    res["elapsed"] = round(time.time() - started, 1)
    if status != "skipped" or "already exists" not in message:
        log_event(res)
    return res


# ------------------------------------------------------------------ log

def log_event(rec):
    paths.ensure_dirs()
    line = json.dumps(rec, default=str) + "\n"
    with open(paths.MANIFEST, "a", encoding="utf-8") as f:
        f.write(line)


def read_log():
    out = []
    try:
        with open(paths.MANIFEST, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
    except OSError:
        pass
    return out


# ------------------------------------------------------------------ Recycle Bin (originals only, on request)

class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]


def send_to_recycle_bin(path):
    """Recycle Bin only. If Windows can't recycle the file it asks before deleting it for good (WANTNUKEWARNING)."""
    FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI, FOF_WANTNUKEWARNING = \
        3, 0x4, 0x10, 0x40, 0x400, 0x4000
    op = _SHFILEOPSTRUCTW(None, FO_DELETE, os.path.abspath(path) + "\0", None,
                          FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI | FOF_WANTNUKEWARNING,
                          False, None, None)
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    return rc == 0 and not op.fAnyOperationsAborted and not os.path.exists(path)


def recycle_check(src, log=None):
    """May this original go to the Recycle Bin? Only when a checked conversion of exactly this file exists."""
    src = os.path.abspath(src)
    log = log if log is not None else read_log()
    try:
        st = os.stat(src)
    except OSError:
        return {"ok": False, "source": src, "reason": "original not found"}
    for rec in reversed(log):
        if rec.get("event") != "convert" or os.path.normcase(rec.get("source", "")) != os.path.normcase(src):
            continue
        if rec.get("status") != "done" or not rec.get("verified"):
            continue
        out = rec.get("output")
        if not out or not os.path.exists(out):
            continue
        if size_of(out) != rec.get("output_size"):
            continue
        if st.st_size != rec.get("source_size") or abs(st.st_mtime - (rec.get("source_mtime") or 0)) > 2:
            return {"ok": False, "source": src, "reason": "the original changed after it was converted"}
        return {"ok": True, "source": src, "size": st.st_size, "output": out, "output_size": rec["output_size"],
                "preset": rec.get("preset"), "reason": "checked conversion: %s" % os.path.basename(out)}
    return {"ok": False, "source": src, "reason": "no checked conversion of this file in the log"}


def recycle(src):
    chk = recycle_check(src)
    if not chk["ok"]:
        return dict(chk, status="kept")
    ok = send_to_recycle_bin(src)
    rec = {"event": "recycle", "time": datetime.datetime.now().isoformat(timespec="seconds"), "source": chk["source"],
           "size": chk["size"], "output": chk["output"], "status": "recycled" if ok else "failed"}
    log_event(rec)
    return dict(chk, status=rec["status"])


# ------------------------------------------------------------------ size estimate (measured on a 5.7K survey)

# MB per frame at 5760x2880 (16.6 MP) by codec and crf, all-intra, from a 5.7K outdoor 360 video
_MB_PER_FRAME = {"hevc": {24: 0.89, 28: 0.71, 30: 0.63, 32: 0.55, 36: 0.42},
                 "h264": {18: 1.96, 22: 1.34, 26: 0.94, 28: 0.78, 30: 0.65},
                 "av1": {14: 0.85, 18: 0.70, 21: 0.63, 24: 0.57, 30: 0.40, 36: 0.28}}
_JPEG_MB = {80: 1.50, 88: 1.99, 92: 2.37}
_AUDIO_KBPS = {"none": 0, "speech": 48, "standard": 96}


def _interp(table, x):
    keys = sorted(table)
    if x <= keys[0]:
        return table[keys[0]] * (1.12 ** (keys[0] - x))
    if x >= keys[-1]:
        return table[keys[-1]] * (0.94 ** (x - keys[-1]))
    for a, b in zip(keys, keys[1:]):
        if a <= x <= b:
            return table[a] + (table[b] - table[a]) * (x - a) / float(b - a)
    return table[keys[-1]]


def estimate_output(info, p):
    """Rough output size in bytes (upper bound when repeats are skipped). None if the video is unknown."""
    if not info.get("width") or not info.get("duration"):
        return None
    width, height = out_size(p, info["width"], info["height"])
    scale = (width * height / (5760.0 * 2880.0)) ** 0.72
    frames = int(info["duration"] // p["interval"]) + 1
    if p["output"] == "photos":
        per = _interp(_JPEG_MB, p["jpeg_quality"]) * scale
        return int(frames * per * 1e6)
    if p["output"] == "chapters":
        # measured: 8K X6 export, NVENC p7: 4.57 MB per second of video at qp 22, 58 % of that at qp 26
        rate = 2.65 * (width * height / (7680.0 * 3840.0)) ** 0.72
        rate *= 0.873 ** (p["video_qp"] - 26)
        audio = 0
        if p["audio"] != "none" and info.get("audio"):
            kbps = _AUDIO_KBPS.get(p["audio"]) or ((info["audio"].get("bitrate") or 192000) / 1000.0)
            audio = kbps * 1000 / 8 * info["duration"]
        return int(rate * 1e6 * info["duration"] + audio)
    per = _interp(_MB_PER_FRAME[p["codec"]], p["crf"]) * scale
    if p["keyframe_every"] > 1:
        per *= 0.6                                       # rough: inter frames between keyframes
    audio = 0
    if p["timing"] == "real" and p["audio"] != "none" and info.get("audio"):
        kbps = _AUDIO_KBPS.get(p["audio"]) or ((info["audio"].get("bitrate") or 192000) / 1000.0)
        audio = kbps * 1000 / 8 * info["duration"]
    return int(frames * per * 1e6 + audio)


# ------------------------------------------------------------------ finding 360 videos

SKIP_DIRS = {"windows", "program files", "program files (x86)", "programdata", "$recycle.bin",
             "system volume information", "appdata", "node_modules", ".git", "__pycache__", "$windows.~bt",
             "$windows.~ws", "recovery", "msocache"}


def scan(roots, min_mb=20, include_appdata=False, progress=None, cancel=None):
    """Probe every video of at least min_mb under the roots. Online-only files are listed, not opened."""
    found = []
    skip = set(SKIP_DIRS)
    if include_appdata:
        skip.discard("appdata")
    own = os.path.normcase(paths.DATA_DIR)
    for root in roots:
        skip_own = not os.path.normcase(os.path.abspath(root)).startswith(own)   # unless asked to look there
        for dirpath, dirnames, files in os.walk(root):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            dirnames[:] = [d for d in dirnames if d.lower() not in skip
                           and not (skip_own and os.path.normcase(os.path.join(dirpath, d)).startswith(own))]
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if ext not in VIDEO_EXT and ext not in CAMERA_RAW_EXT:
                    continue
                full = os.path.join(dirpath, f)
                try:
                    if os.path.getsize(full) < min_mb * 1024 * 1024:
                        continue
                except OSError:
                    continue
                if progress:
                    progress(len(found), full)
                found.append(probe(full))
    return found
