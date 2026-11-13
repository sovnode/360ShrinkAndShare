"""Conversion presets: what each one makes, how often, how big. Stored in presets.json in the data folder (see paths.py)."""
import copy
import json
import os
import re

import paths

# key: (type, allowed values or (min, max), default)
FIELDS = {
    "name":           (str, None, "New preset"),
    "description":    (str, None, ""),
    "output":         (str, ("video", "photos", "chapters"), "video"),
    "interval":       (float, (0.5, 3600.0), 5.0),       # seconds between panoramas
    "width":          ("width", (640, 16384), "original"),  # "original" or pixels (height follows the source)
    "pick":           (str, ("sharpest", "nearest"), "sharpest"),
    "search":         (str, ("auto", "keyframes", "every"), "auto"),
    "skip_unchanged": (bool, None, False),
    # Each panorama is shown a fixed 1.5 s by default (whatever the interval). At this pace VLC's
    # next-frame key E keeps stepping; with real time (5 s a panorama) VLC 3 stops it after 2-3 panoramas.
    "timing":         (str, ("real", "hold"), "hold"),
    "hold":           (float, (0.2, 600.0), 1.5),        # seconds per panorama when timing = hold
    "codec":          (str, ("hevc", "h264", "av1"), "hevc"),
    "crf":            (int, (0, 63), 30),
    "speed":          (str, ("fast", "medium", "slow"), "fast"),     # CPU encoders only
    "encoder":        (str, ("auto", "nvidia", "intel", "cpu"), "auto"),   # GPU first, the CPU when it can't
    "keyframe_every": (int, (1, 600), 1),
    "audio":          (str, ("none", "speech", "standard", "copy"), "none"),   # sound is off unless a preset turns it on
    "jpeg_quality":   (int, (30, 100), 88),
    # 12 fps video (1.2): the quantizer of every frame (lower = better, bigger). One setting on purpose: with softer
    # frames between the chapter frames, the whole video looks soft while it plays.
    "video_qp":       (int, (10, 51), 26),
    "menu":           (bool, None, True),                # show in the Explorer right-click menu
}

LABELS = {
    "output": {"video": "Video (360 slideshow .mp4)", "photos": "Photos (360 JPEG panoramas)",
               "chapters": "12 fps video (whole video, a chapter on the sharpest frame)"},
    "pick": {"sharpest": "Sharpest frame in each interval", "nearest": "Frame nearest each interval mark"},
    "search": {"auto": "Auto", "keyframes": "Keyframes only (fast)", "every": "Every 0.5 s (thorough, slow)"},
    "timing": {"real": "Real time (timeline matches the original; can have sound)",
               "hold": "Fixed time per panorama (default 1.5 s; no sound)"},
    "codec": {"hevc": "HEVC / H.265 (smallest)", "h264": "H.264 (plays anywhere)", "av1": "AV1 (fast to make)"},
    "speed": {"fast": "Fast", "medium": "Medium", "slow": "Slow (a little smaller)"},
    "encoder": {"auto": "Automatic: the GPU when there is one (fast)", "nvidia": "NVIDIA GPU (NVENC)",
                "intel": "Intel GPU (Quick Sync)", "cpu": "CPU only (slow, as in version 1.0)"},
    "audio": {"none": "No sound", "speech": "Speech (mono 48 kbps)", "standard": "Standard (stereo 96 kbps)",
              "copy": "Original sound (copied unchanged)"},
}

CRF_HINT = {"hevc": "HEVC 24 (best) - 34 (smallest); 30 is visually lossless for most surveys",
            "h264": "H.264 18 (best) - 30 (smallest); 24 is a good middle",
            "av1": "AV1 16 (best) - 32 (smallest); 22 is a good middle"}

NAME_RE = re.compile(r"^[A-Za-z0-9 ,.()+&'_-]{1,60}$")


def _p(**kw):
    d = {k: v[2] for k, v in FIELDS.items()}
    d.update(kw)
    return d


DEFAULTS = [            # slideshows: 1.5 s per panorama, silent, GPU = auto; every preset picks the sharpest frame
    _p(name="Reduce frame rate to 12fps", output="chapters", interval=2.0, speed="medium", audio="copy",
       encoder="nvidia",
       description="Keeps the whole video at 12 frames a second with its sound, every frame at qp 26 on the NVIDIA "
                   "GPU. The sharpest frame of every 2 s gets a chapter: pause, then Shift+N / Shift+P in VLC."),
    _p(name="Slideshow movie full quality", audio="none",
       description="One panorama every 5 s at the video's own resolution (up to 8K), HEVC, each shown 1.5 s. "
                   "Measured: 1/59 of a 5.7K survey, 1/174 of a 4K one."),
    _p(name="Slideshow movie low quality", width=2880, crf=32, skip_unchanged=True, audio="none",
       description="One panorama every 5 s at 2880 px, HEVC, each shown 1.5 s; skips repeats when the camera "
                   "stands still."),
    _p(name="Slideshow high compatibility", codec="h264", crf=24, audio="none",
       description="One panorama every 5 s at the video's own resolution, H.264 instead of HEVC, for PCs that "
                   "can't play HEVC; each shown 1.5 s."),
    _p(name="Converter to 360 photos", output="photos", audio="none",
       description="A folder of 360 JPEG panoramas at the video's own resolution (quality 88), one every 5 s."),
]


def normalize(p):
    """Coerce a preset dict to the schema; unknown keys are dropped, missing ones get defaults."""
    out = {}
    if "video_qp" not in p and "chapter_qp" in p:     # presets saved before the two settings became one
        p = dict(p, video_qp=p["chapter_qp"])
    for key, (typ, rng, default) in FIELDS.items():
        v = p.get(key, default)
        try:
            if typ is bool:
                v = v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")
            elif typ is int:
                v = int(round(float(v)))
            elif typ is float:
                v = float(v)
            elif typ == "width":
                s = str(v).strip().lower()
                v = "original" if s in ("original", "orig", "full", "source", "") else int(round(float(s)))
            else:
                v = str(v).strip()
                if isinstance(rng, tuple) and rng and isinstance(rng[0], str):
                    v = v.lower()
        except (TypeError, ValueError):
            v = default
        out[key] = v
    return out


def validate(p):
    """-> list of problems with one (normalized) preset."""
    errs = []
    if not NAME_RE.match(p["name"]):
        errs.append("Name: 1-60 letters, digits, spaces and , . ( ) + & ' _ - only")
    for key, (typ, rng, _d) in FIELDS.items():
        v = p[key]
        if isinstance(rng, tuple) and rng and isinstance(rng[0], str) and v not in rng:
            errs.append("%s: must be one of %s" % (key, ", ".join(rng)))
        elif typ in (int, float) and isinstance(rng, tuple) and not (rng[0] <= v <= rng[1]):
            errs.append("%s: must be between %g and %g" % (key, rng[0], rng[1]))
        elif typ == "width" and v != "original" and not (rng[0] <= v <= rng[1]):
            errs.append("width: 'original' or %d-%d px" % rng)
    if p["output"] == "chapters" and p["interval"] > 5.0:
        errs.append("interval: at most 5 s for a chaptered video (each interval's frames are held in memory)")
    if p["output"] == "video":
        top = {"hevc": 51, "h264": 51, "av1": 63}[p["codec"]]
        if p["crf"] > top:
            errs.append("crf: at most %d for %s" % (top, p["codec"]))
    return errs


def load():
    paths.ensure_dirs()
    try:
        with open(paths.PRESETS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        items = [normalize(p) for p in data.get("presets", [])]
        if items:
            if data.get("version", 1) < 2:            # 1.2 adds the chaptered-video preset once
                if not any(p["output"] == "chapters" for p in items):
                    names = {p["name"].lower() for p in items}
                    items += [copy.deepcopy(p) for p in DEFAULTS
                              if p["output"] == "chapters" and p["name"].lower() not in names]
                items = save(items)
            return items
    except (OSError, ValueError, AttributeError):
        pass
    items = copy.deepcopy(DEFAULTS)
    save(items)
    return items


def save(items):
    items = [normalize(p) for p in items]
    names = set()
    for p in items:
        errs = validate(p)
        if errs:
            raise ValueError("Preset '%s': %s" % (p["name"], "; ".join(errs)))
        if p["name"].lower() in names:
            raise ValueError("Two presets are called '%s'" % p["name"])
        names.add(p["name"].lower())
    paths.ensure_dirs()
    tmp = paths.PRESETS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": 2, "presets": items}, f, indent=2)
    os.replace(tmp, paths.PRESETS_FILE)
    return items


def find(name, items=None):
    items = items if items is not None else load()
    for p in items:
        if p["name"].lower() == str(name).strip().lower():
            return p
    for p in items:                                   # unique prefix, e.g. "standard"
        if p["name"].lower().startswith(str(name).strip().lower()):
            return p
    raise KeyError("No preset called '%s'. Presets: %s" % (name, "; ".join(p["name"] for p in items)))


def apply_overrides(p, pairs):
    """pairs like ['interval=2', 'width=3840'] -> new validated preset."""
    q = dict(p)
    for pair in pairs or ():
        if "=" not in pair:
            raise ValueError("--set needs key=value, got '%s'" % pair)
        k, v = pair.split("=", 1)
        k = k.strip()
        if k not in FIELDS:
            raise ValueError("unknown setting '%s' (known: %s)" % (k, ", ".join(FIELDS)))
        q[k] = v
    q = normalize(q)
    errs = validate(q)
    if errs:
        raise ValueError("; ".join(errs))
    return q


def fmt_num(x):
    return ("%g" % x) if x != int(x) else str(int(x))


def summary(p):
    res = "full resolution" if p["width"] == "original" else "%d px" % p["width"]
    if p["output"] == "photos":
        return "JPEG panoramas every %s s, %s, quality %d" % (fmt_num(p["interval"]), res, p["jpeg_quality"])
    if p["output"] == "chapters":
        where = {"auto": "GPU when available", "nvidia": "NVIDIA GPU", "intel": "Intel GPU", "cpu": "CPU"}[p["encoder"]]
        sound = LABELS["audio"][p["audio"]].split(" (")[0].lower()
        return ("Whole video at 12 fps, a chapter on the sharpest frame every %s s, %s, HEVC qp %d (%s), %s"
                % (fmt_num(p["interval"]), res, p["video_qp"], where, sound))
    timing = "real time" if p["timing"] == "real" else "%s s each" % fmt_num(p["hold"])
    sound = LABELS["audio"][p["audio"]].split(" (")[0].lower() if p["timing"] == "real" else "no sound"
    where = {"auto": "GPU when available", "nvidia": "NVIDIA GPU", "intel": "Intel GPU", "cpu": "CPU"}[p["encoder"]]
    if p["codec"] == "av1":
        where = "CPU"                                  # no GPU here encodes AV1
    return "Slideshow, one panorama every %s s, %s, %s crf %d (%s), %s, %s" % (
        fmt_num(p["interval"]), res, p["codec"].upper(), p["crf"], where, timing, sound)
