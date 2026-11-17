"""Browser pages: pack a 360 video, or a set of 360 photos, into ONE .html file that opens in Edge or Chrome
by double-click, with nothing to install and no internet (version 1.3).

Why one file with everything inside: a page opened from disk may not put a picture or video from a file
beside it into a 360 view (the browser treats every local file as another site and blocks it). Data that
is inside the page itself is allowed. So the video (or each photo) is written into the page as base64 text,
in pieces, and the viewer (viewer.html) rebuilds it in memory.

    pack_video(mp4, out_html)                     a 360 video or 360 slideshow -> a page that plays it
    pack_photos([jpg, ...], out_html, title)      360 photos -> an album page (arrow keys, thumbnails)
    pack(path)                                    either of the two, from a .mp4 or a folder of photos
    verify(html, source)                          the page holds exactly the source's bytes
"""
import base64
import hashlib
import html as htmllib
import io
import json
import os
import re

import paths

paths.add_pylib()

import av                    # noqa: E402
from PIL import Image        # noqa: E402

VIEWER = os.path.join(paths.APP_DIR, "viewer.html")
CHUNK = 6 * 1024 * 1024                  # bytes of video per piece (a multiple of 3: base64 without padding)
PHOTO_EXT = (".jpg", ".jpeg", ".png")
THUMB = (256, 128)
# The whole page is held in the browser's memory while it opens. 1.5 GB of video opened and played in Edge and
# Chrome 157; 3 GB did not open. Above this the engine keeps the .mp4 instead.
MAX_VIDEO_BYTES = int(float(os.environ.get("V360_HTML_MAX_GB", "1.5")) * 1024 ** 3)
STREAM_PIECE = 4 * 1024 * 1024           # streaming pieces: whole fragments, grouped up to this size
WHOLE_FILE_MAX = 400 * 1024 * 1024      # a video that can't be cut into pieces: Chrome 157 fails above ~500 MB
ALBUM_MAX_WIDTH = 8192                  # photos straight from a camera (up to 72 MP, 20 MB each) are reduced to this
ALBUM_QUALITY = 85
CODEC_NAMES = {"hevc": "HEVC video", "h264": "H.264 video", "av1": "AV1 video", "vp9": "VP9 video"}
Image.MAX_IMAGE_PIXELS = None            # 360 photos are big (72 MP and more); these are the user's own files


class NotPanorama(ValueError):
    pass


def _template():
    with open(VIEWER, encoding="utf-8") as f:
        text = f.read()
    head, tail = text.split("<!--DATA-->")
    return head, tail


def _head(meta, title):
    head, tail = _template()
    meta = dict(meta, app=paths.APP_NAME, version=paths.VERSION)
    js = json.dumps(meta, ensure_ascii=True, separators=(",", ":")).replace("<", "\\u003c")
    head = (head.replace("{{TITLE}}", htmllib.escape(title))
            .replace("{{GENERATOR}}", htmllib.escape("%s %s" % (paths.APP_NAME, paths.VERSION)))
            .replace("{{META}}", js))
    return head, tail


def _piece(f, ident, data):
    f.write(b'<script type="application/octet-stream" class="d" id="d%d">' % ident)
    f.write(base64.b64encode(data))
    f.write(b"</script>\n")


def _mmss(t):
    t = int(t or 0)
    return "%d:%02d:%02d" % (t // 3600, t % 3600 // 60, t % 60) if t >= 3600 else "%d:%02d" % (t // 60, t % 60)


def _human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%.0f %s" if unit in ("B", "KB") or n >= 100 else "%.1f %s") % (n, unit)
        n /= 1024.0


# ------------------------------------------------------------------ video

def video_meta(mp4):
    """What the viewer needs to know about a video: size, length, chapters, sound."""
    with av.open(mp4) as c:
        vs = c.streams.video[0]
        cc = vs.codec_context
        dur = float(vs.duration * vs.time_base) if vs.duration else (c.duration / 1e6 if c.duration else 0.0)
        frames = vs.frames or 0
        chapters = [{"t": round(float(ch["start"] * ch["time_base"]), 3),
                     "title": (ch.get("metadata") or {}).get("title", "")} for ch in c.chapters()]
        title = c.metadata.get("title") or os.path.splitext(os.path.basename(mp4))[0]
        rate = vs.average_rate
        meta = {"mime": "video/mp4", "bytes": os.path.getsize(mp4), "duration": round(dur, 3),
                "width": cc.width, "height": cc.height, "codec": CODEC_NAMES.get(cc.name, cc.name),
                "codec_id": cc.name, "profile": cc.profile or "", "fps": round(float(rate), 3) if rate else None,
                "audio": bool(c.streams.audio), "chapters": chapters,
                # a slideshow: every frame is a panorama that stays up (under 2 pictures a second)
                "stills": bool(dur and frames and frames / dur < 2.0)}
    return title, meta


def browser_warning(meta):
    """A reason this video may not play in every browser, or ''."""
    if meta.get("codec_id") == "hevc" and "intra" in (meta.get("profile") or "").lower():
        return ("made by version 1.0 as HEVC 'Main Intra', which Edge can't play: convert the original again, "
                "or use Chrome")
    if meta.get("codec_id") == "hevc":
        return ""
    return ""


def _fragment(mp4, out):
    """Copy the video (and AAC sound) into a 'fragmented' MP4: the same pictures, cut into independent pieces
    that each start on a keyframe, which is the form a browser can be fed a few seconds at a time.
    Nothing is re-encoded. -> (video packets copied, has sound)."""
    n = 0
    with av.open(mp4) as ic, av.open(out, "w", format="mp4",
                                     options={"movflags": "frag_keyframe+empty_moov+default_base_moof"}) as oc:
        vs = ic.streams.video[0]
        outs = {vs.index: oc.add_stream_from_template(vs)}
        sound = next((a for a in ic.streams.audio if a.codec_context.name == "aac"), None)
        if sound is not None:
            outs[sound.index] = oc.add_stream_from_template(sound)
        for pkt in ic.demux([s for s in ic.streams if s.index in outs]):
            if pkt.dts is None or pkt.size == 0:
                continue
            if pkt.stream.index == vs.index:
                n += 1
            pkt.stream = outs[pkt.stream.index]
            oc.mux(pkt)
    return n, sound is not None


def _boxes(path):
    """Top-level MP4 boxes -> [(type, offset, size)]."""
    out, size = [], os.path.getsize(path)
    with open(path, "rb") as f:
        off = 0
        while off + 8 <= size:
            f.seek(off)
            head = f.read(16)
            n, typ = int.from_bytes(head[:4], "big"), head[4:8]
            if n == 1:
                n = int.from_bytes(head[8:16], "big")
            elif n == 0:
                n = size - off
            if n < 8:
                raise ValueError("broken MP4 box at %d" % off)
            out.append((typ, off, n))
            off += n
    return out


def _codec_string(init):
    """The browser's name for the video format, from the MP4 header (e.g. hvc1.1.6.L183.B0, avc1.640033), or None."""
    i = init.find(b"stsd")
    while i >= 0:
        four = init[i + 16:i + 20]
        if four in (b"hvc1", b"hev1"):
            k = init.find(b"hvcC", i)
            if k < 0:
                return None
            c = init[k + 4:k + 17]
            space, tier, profile = c[1] >> 6, (c[1] >> 5) & 1, c[1] & 31
            compat = int("{:032b}".format(int.from_bytes(c[2:6], "big"))[::-1], 2)
            cons = bytes(c[6:12]).rstrip(b"\0")
            return "%s.%s%d.%X.%s%d%s" % (four.decode(), ("", "A", "B", "C")[space], profile, compat,
                                          "H" if tier else "L", c[12], "".join(".%02X" % b for b in cons))
        if four in (b"avc1", b"avc3"):
            k = init.find(b"avcC", i)
            if k < 0:
                return None
            c = init[k + 4:k + 8]
            return "%s.%02x%02x%02x" % (four.decode(), c[1], c[2], c[3])
        i = init.find(b"stsd", i + 4)
    return None


def _segments(frag):
    """-> (length of the header, [[start time s, offset, size], ...] one per fragment that starts with video,
    time of the first picture)."""
    boxes = _boxes(frag)
    moofs = [(off, n) for typ, off, n in boxes if typ == b"moof"]
    if not moofs:
        raise ValueError("the video could not be cut into pieces")
    end = next((off for typ, off, n in boxes if typ == b"mfra"), os.path.getsize(frag))
    starts = [off for off, _ in moofs] + [end]
    first = {}
    with av.open(frag) as c:
        vs = c.streams.video[0]
        k = 0
        for pkt in c.demux(vs):
            if pkt.pts is None or pkt.pos is None or pkt.pos < 0:
                continue
            while k + 1 < len(moofs) and pkt.pos >= starts[k + 1]:
                k += 1
            t = float(pkt.pts * vs.time_base)
            if k not in first or t < first[k]:
                first[k] = t
    if 0 not in first:
        raise ValueError("no pictures found in the first piece of the video")
    t0 = min(first.values())
    segs = []
    for k in range(len(moofs)):
        size = starts[k + 1] - starts[k]
        if k in first:
            segs.append([first[k] - t0, starts[k], size])
        else:                                    # a piece with sound only: it goes with the piece before it
            segs[-1][2] += size
    return moofs[0][0], segs, t0


def pack_video(mp4, out_html, title=None, source_name=None):
    """-> info dict. The pictures and sound go into the page as they are (no re-encoding)."""
    size = os.path.getsize(mp4)
    if size > MAX_VIDEO_BYTES:
        raise ValueError("%s is too big for a browser page (at most about %s): use a slideshow or photo-album "
                         "preset, or a smaller width" % (_human(size), _human(MAX_VIDEO_BYTES)))
    vtitle, meta = video_meta(mp4)
    title = title or vtitle
    what = "360 slideshow, %d panoramas" % len(meta["chapters"]) if meta["stills"] and meta["chapters"] else \
        "360 video, %s" % _mmss(meta["duration"])
    sub = "%s · %d × %d" % (what, meta["width"], meta["height"])
    if source_name:
        sub += " · from %s" % source_name
    sha = hashlib.sha256()
    tmp = out_html + ".part"
    frag = out_html + ".frag"
    try:
        # the streaming form (pieces fed to the browser a few seconds at a time: no size limit in the browser
        # beyond the page itself); a format it can't be made for goes in whole, which Chrome limits to ~450 MB
        plan = None
        try:
            packets, sound = _fragment(mp4, frag)
            init_len, segs, t0 = _segments(frag)
            with open(frag, "rb") as f:
                codec = _codec_string(f.read(init_len))
            if codec and packets:
                plan = [(0, init_len)]
                times = []
                for t, off, n in segs:               # whole fragments, grouped into pieces of up to about 4 MB
                    last_off, last_n = plan[-1]
                    if times and last_n + n <= STREAM_PIECE and last_off + last_n == off:
                        plan[-1] = (last_off, last_n + n)
                    else:
                        plan.append((off, n))
                        times.append(round(t, 4))
                meta.update(mse='video/mp4; codecs="%s%s"' % (codec, ", mp4a.40.2" if sound else ""),
                            pieces=[[t, n] for t, (_o, n) in zip(times, plan[1:])], audio=sound,
                            t0=round(t0, 5),
                            bytes=sum(n for _o, n in plan))
        except (ValueError, OSError, av.FFmpegError):
            plan = None
        if plan is None and size > WHOLE_FILE_MAX:
            raise ValueError("this video's format can only go into a page whole, and at %s it is over the %s "
                             "that Chrome can open that way" % (_human(size), _human(WHOLE_FILE_MAX)))
        head, tail = _head({"kind": "video", "title": title, "subtitle": sub, "video": meta}, title)
        total = 0
        with open(frag if plan else mp4, "rb") as src, open(tmp, "wb") as f:
            f.write(head.encode("utf-8"))
            i = 0
            if plan:
                for off, n in plan:
                    src.seek(off)
                    data = src.read(n)
                    sha.update(data)
                    _piece(f, i, data)
                    total += len(data)
                    i += 1
            else:
                while True:
                    data = src.read(CHUNK)
                    if not data:
                        break
                    sha.update(data)
                    _piece(f, i, data)
                    total += len(data)
                    i += 1
            f.write(tail.encode("utf-8"))
        os.replace(tmp, out_html)
    finally:
        for p in (frag, tmp):
            if os.path.exists(p):
                os.remove(p)
    return {"kind": "video", "pieces": i, "bytes": total, "sha256": sha.hexdigest(), "title": title,
            "chapters": len(meta["chapters"]), "warning": browser_warning(meta), "meta": meta,
            "streaming": bool(plan)}


# ------------------------------------------------------------------ photos

def _label(path):
    stem = os.path.splitext(os.path.basename(path))[0]
    m = re.match(r"^(\d{4,})s(?: \d+)? 360$", stem)          # this app's own names: "0125s 360.jpg" -> 2:05
    return _mmss(int(m.group(1))) if m else stem


def _photo_bytes(path, width, quality):
    """-> (jpeg bytes, (w, h), thumbnail data URI). The file's own bytes when it is a JPEG that needs no resizing."""
    with Image.open(path) as im:
        w, h = im.size
        if w < 1000 or abs(w - 2 * h) > max(8, w // 100):
            raise NotPanorama("%s is %dx%d, not a 360 photo (they are twice as wide as high)"
                              % (os.path.basename(path), w, h))
        fmt = im.format
        im.draft("RGB", (THUMB[0] * 2, THUMB[1] * 2))        # JPEG: decode small for the thumbnail
        th = im.convert("RGB").resize(THUMB, Image.BILINEAR)
        buf = io.BytesIO()
        th.save(buf, "JPEG", quality=55, optimize=True)
        thumb = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    if fmt == "JPEG" and (not width or w <= width):
        with open(path, "rb") as f:
            return f.read(), (w, h), thumb
    with Image.open(path) as im:
        im = im.convert("RGB")
        if width and w > width:
            w2 = width - width % 2
            im = im.resize((w2, max(2, int(round(w2 * h / w / 2.0)) * 2)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality or ALBUM_QUALITY, optimize=True)
        return buf.getvalue(), im.size, thumb


def list_photos(folder):
    return sorted((os.path.join(folder, f) for f in os.listdir(folder) if f.lower().endswith(PHOTO_EXT)),
                  key=lambda p: os.path.basename(p).lower())


def pack_photos(photos, out_html, title, width=None, quality=None, source_name=None, progress=None):
    """-> info dict. photos: paths in viewing order. width: reduce wider photos to this (None = as they are)."""
    photos = list(photos)
    if not photos:
        raise ValueError("no photos to put in the album")
    sha = hashlib.sha256()
    items, skipped, total, sizes = [], [], 0, set()
    tmp = out_html + ".part"
    body = out_html + ".body"
    try:
        with open(body, "wb") as f:
            for n, path in enumerate(photos):
                try:
                    data, size, thumb = _photo_bytes(path, width, quality)
                except NotPanorama as e:
                    skipped.append(str(e))
                    continue
                sha.update(data)
                _piece(f, len(items), data)
                items.append({"label": _label(path), "thumb": thumb})
                total += len(data)
                sizes.add(size)
                if progress:
                    progress((n + 1) / float(len(photos)), "%d of %d photos packed" % (n + 1, len(photos)))
        if not items:
            raise ValueError("none of the %d pictures is a 360 photo: %s" % (len(photos), skipped[0]))
        big = max(sizes)
        sub = "%d 360 photo%s · %d × %d" % (len(items), "" if len(items) == 1 else "s", big[0], big[1])
        if source_name:
            sub += " · from %s" % source_name
        head, tail = _head({"kind": "photos", "title": title, "subtitle": sub, "items": items}, title)
        with open(tmp, "wb") as f, open(body, "rb") as b:
            f.write(head.encode("utf-8"))
            while True:
                data = b.read(8 * 1024 * 1024)
                if not data:
                    break
                f.write(data)
            f.write(tail.encode("utf-8"))
        os.replace(tmp, out_html)
    finally:
        for p in (body, tmp):
            if os.path.exists(p):
                os.remove(p)
    return {"kind": "photos", "pieces": len(items), "bytes": total, "sha256": sha.hexdigest(), "title": title,
            "skipped": skipped, "warning": ""}


# ------------------------------------------------------------------ either, from what is on disk

def default_output(path, out_dir=None):
    path = os.path.abspath(path)
    stem = os.path.basename(path) if os.path.isdir(path) else os.path.splitext(os.path.basename(path))[0]
    stem = re.sub(r"\(360 frames ", "(360 album ", stem)
    return os.path.join(out_dir or os.path.dirname(path), stem + ".html")


def pack(path, out_html=None, out_dir=None, width="auto", quality=None, progress=None):
    """A .mp4 (360 video or slideshow) or a folder of 360 photos -> one .html beside it. Never overwrites.
    width (photos only): 'auto' = reduce photos wider than 8192 px, None = as they are, or pixels."""
    path = os.path.abspath(path)
    out_html = out_html or default_output(path, out_dir)
    if os.path.exists(out_html):
        raise FileExistsError("already there: %s" % out_html)
    if os.path.isdir(path):
        photos = list_photos(path)
        if not photos:
            raise ValueError("no .jpg or .png photos in %s" % path)
        title = re.sub(r" \(360 frames [^)]*\)$", "", os.path.basename(path))
        info = pack_photos(photos, out_html, title, width=ALBUM_MAX_WIDTH if width == "auto" else width,
                           quality=quality, progress=progress)
    elif os.path.splitext(path)[1].lower() in (".mp4", ".m4v", ".mov"):
        info = pack_video(path, out_html)
    else:
        raise ValueError("%s is not a .mp4 or a folder of photos" % path)
    info["output"] = out_html
    info["output_size"] = os.path.getsize(out_html)
    return info


# ------------------------------------------------------------------ checking a page

_PIECE = re.compile(rb'^<script type="application/octet-stream" class="d" id="d(\d+)">([A-Za-z0-9+/=]*)</script>\s*$')
_META = re.compile(rb'<script type="application/json" id="meta">(.*?)</script>')


def read_packed(html_path):
    """-> (meta, pieces, bytes, sha256 of everything packed in the page)."""
    sha = hashlib.sha256()
    meta, n, total = None, 0, 0
    with open(html_path, "rb") as f:
        for line in f:
            if meta is None:
                m = _META.search(line)
                if m:
                    meta = json.loads(m.group(1).decode("utf-8"))
                    continue
            m = _PIECE.match(line)
            if m:
                if int(m.group(1)) != n:
                    raise ValueError("piece %d is out of order" % n)
                data = base64.b64decode(m.group(2), validate=True)
                sha.update(data)
                n += 1
                total += len(data)
    if meta is None:
        raise ValueError("not a page made by %s" % paths.APP_NAME)
    return meta, n, total, sha.hexdigest()


def verify(html_path, info):
    """The page holds exactly what was packed (info from pack_video / pack_photos) and ends properly."""
    problems = []
    try:
        meta, n, total, digest = read_packed(html_path)
    except (OSError, ValueError) as e:
        return {"ok": False, "problem": "the page can't be read back: %s" % e}
    if n != info["pieces"] or total != info["bytes"] or digest != info["sha256"]:
        problems.append("the page holds %d pieces / %d bytes, expected %d / %d" % (n, total, info["pieces"],
                                                                                info["bytes"]))
    if meta.get("kind") != info["kind"]:
        problems.append("wrong kind of page")
    if info["kind"] == "photos" and len(meta.get("items") or ()) != n:
        problems.append("%d photos listed, %d packed" % (len(meta.get("items") or ()), n))
    with open(html_path, "rb") as f:
        f.seek(max(0, os.path.getsize(html_path) - 64))
        if b"</html>" not in f.read():
            problems.append("the page is cut short")
    return {"ok": not problems, "problem": "; ".join(problems), "pieces": n, "bytes": total}
