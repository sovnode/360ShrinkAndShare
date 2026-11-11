"""MP4 / MOV box helpers: read and write 360 (spherical) metadata, read keyframe spacing.

Players such as VLC switch to their look-around 360 view only when the file says it is 360:
  V1  a 'uuid' box (ffcc8263-f855-4a93-8814-587a02521fdd) in the video 'trak' holding Google's
      GSpherical XML (ProjectionType equirectangular, StereoMode mono / top-bottom / left-right)
  V2  'st3d' (stereo mode) and 'sv3d' > 'svhd' + 'proj' > 'prhd' + 'equi' inside the video
      sample entry (avc1 / hvc1 / av01 ...), as written by Insta360 Studio and FFmpeg.
inject_spherical() writes both, like Insta360 Studio does. Only the 'moov' box is rewritten: when it
is the last box (FFmpeg's default) the file is patched in place; otherwise the file is rewritten and
the chunk offsets are moved.
"""
import os
import re
import shutil
import struct

V1_UUID = bytes.fromhex("ffcc8263f8554a938814587a02521fdd")
CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"dinf", b"mvex", b"sv3d", b"proj"}
VISUAL_ENTRIES = {b"avc1", b"avc3", b"hvc1", b"hev1", b"av01", b"vp08", b"vp09", b"mp4v", b"jpeg",
                  b"mjpa", b"mjpb", b"apcn", b"apch", b"apcs", b"apco", b"ap4h", b"dvh1", b"dvhe"}
VISUAL_ENTRY_PREFIX = 78          # SampleEntry (8) + VisualSampleEntry fields (70) before child boxes
STEREO_CODES = {"mono": 0, "top-bottom": 1, "left-right": 2}
STEREO_NAMES = {v: k for k, v in STEREO_CODES.items()}


class Box:
    """A box. Containers keep 'prefix' (bytes before the children) and 'children'; leaves keep 'data'."""
    __slots__ = ("type", "data", "prefix", "children", "tail")

    def __init__(self, type_, data=b"", prefix=b"", children=None, tail=b""):
        self.type, self.data, self.prefix, self.children, self.tail = type_, data, prefix, children, tail

    def serialize(self):
        if self.children is None:
            body = self.data
        else:
            body = self.prefix + b"".join(c.serialize() for c in self.children) + self.tail
        size = 8 + len(body)
        if size > 0xFFFFFFFF:
            return struct.pack(">I4sQ", 1, self.type, size + 8) + body
        return struct.pack(">I4s", size, self.type) + body

    def child(self, type_):
        for c in self.children or ():
            if c.type == type_:
                return c
        return None


def _parse(buf, start, end, parent=None):
    """Parse boxes in buf[start:end]. Returns (boxes, unparsed tail bytes)."""
    out = []
    pos = start
    while pos + 8 <= end:
        size, typ = struct.unpack_from(">I4s", buf, pos)
        hlen = 8
        if size == 1:
            if pos + 16 > end:
                break
            size = struct.unpack_from(">Q", buf, pos + 8)[0]
            hlen = 16
        elif size == 0:
            size = end - pos
        if size < hlen or pos + size > end:
            break                                          # not a box: keep the rest verbatim
        body_start, body_end = pos + hlen, pos + size
        if typ in CONTAINERS:
            kids, tail = _parse(buf, body_start, body_end, typ)
            out.append(Box(typ, children=kids, tail=tail))
        elif typ == b"stsd" and body_end - body_start >= 8:
            kids, tail = _parse(buf, body_start + 8, body_end, typ)
            out.append(Box(typ, prefix=bytes(buf[body_start:body_start + 8]), children=kids, tail=tail))
        elif typ in VISUAL_ENTRIES and parent == b"stsd" and body_end - body_start >= VISUAL_ENTRY_PREFIX:
            p = body_start + VISUAL_ENTRY_PREFIX
            kids, tail = _parse(buf, p, body_end, typ)
            out.append(Box(typ, prefix=bytes(buf[body_start:p]), children=kids, tail=tail))
        else:
            out.append(Box(typ, data=bytes(buf[body_start:body_end])))
        pos += size
    return out, bytes(buf[pos:end])


def top_level(f):
    """[(type, offset, size)] of the top-level boxes, reading headers only."""
    f.seek(0, 2)
    end = f.tell()
    out, pos = [], 0
    while pos + 8 <= end:
        f.seek(pos)
        hdr = f.read(16)
        size, typ = struct.unpack_from(">I4s", hdr)
        if size == 1:
            size = struct.unpack_from(">Q", hdr, 8)[0]
        elif size == 0:
            size = end - pos
        if size < 8:
            break
        out.append((typ, pos, size))
        pos += size
    return out, end


def load_moov(path):
    with open(path, "rb") as f:
        boxes, end = top_level(f)
        for typ, off, size in boxes:
            if typ == b"moov":
                f.seek(off)
                raw = f.read(size)
                parsed, _ = _parse(raw, 0, len(raw))
                return parsed[0], off, size, boxes, end
    raise ValueError("no moov box (not an MP4/MOV file?)")


def _handler(trak):
    mdia = trak.child(b"mdia")
    hdlr = mdia.child(b"hdlr") if mdia else None
    return hdlr.data[8:12] if hdlr and len(hdlr.data) >= 12 else b""


def video_trak(moov):
    for c in moov.children:
        if c.type == b"trak" and _handler(c) == b"vide":
            return c
    return None


def sample_entry(trak):
    stbl = _path(trak, b"mdia", b"minf", b"stbl")
    stsd = stbl.child(b"stsd") if stbl else None
    return stsd.children[0] if stsd and stsd.children else None


def _path(box, *types):
    for t in types:
        box = box.child(t) if box is not None else None
    return box


def _xml_tag(xml, name):
    m = re.search(r"<(?:\w+:)?%s>\s*([^<]*?)\s*</" % name, xml)
    return m.group(1) if m else None


def read_spherical(path):
    """-> {'v1': {...} | None, 'v2': {...} | None, 'projection': str | None, 'stereo': str}"""
    info = {"v1": None, "v2": None, "projection": None, "stereo": "mono"}
    try:
        moov = load_moov(path)[0]
    except (OSError, ValueError, struct.error):
        return info
    trak = video_trak(moov)
    if trak is None:
        return info
    for c in trak.children:
        if c.type == b"uuid" and c.data[:16] == V1_UUID:
            xml = c.data[16:].decode("utf-8", "replace")
            sph = (_xml_tag(xml, "Spherical") or "").lower() == "true"
            proj = (_xml_tag(xml, "ProjectionType") or ("equirectangular" if sph else "")).lower() or None
            info["v1"] = {"spherical": sph, "projection": proj, "stereo": (_xml_tag(xml, "StereoMode") or "mono").lower(),
                          "software": _xml_tag(xml, "StitchingSoftware")}
    se = sample_entry(trak)
    if se is not None and se.children is not None:
        v2 = {}
        st3d = se.child(b"st3d")
        if st3d is not None and len(st3d.data) >= 5:
            v2["stereo"] = STEREO_NAMES.get(st3d.data[4], "stereo-%d" % st3d.data[4])
        sv3d = se.child(b"sv3d")
        if sv3d is not None:
            proj = sv3d.child(b"proj")
            kinds = {b"equi": "equirectangular", b"cbmp": "cubemap", b"mshp": "mesh"}
            v2["projection"] = next((kinds[c.type] for c in (proj.children if proj else []) if c.type in kinds), "unknown")
        if v2:
            info["v2"] = v2
    if info["v2"] and info["v2"].get("projection"):
        info["projection"] = info["v2"]["projection"]
    elif info["v1"] and info["v1"]["spherical"]:
        info["projection"] = info["v1"]["projection"] or "equirectangular"
    info["stereo"] = (info["v2"] or {}).get("stereo") or (info["v1"] or {}).get("stereo") or "mono"
    return info


def keyframe_info(path):
    """-> {'frames', 'keyframes', 'duration', 'spacing'} of the video track, or None."""
    try:
        moov = load_moov(path)[0]
    except (OSError, ValueError, struct.error):
        return None
    trak = video_trak(moov)
    if trak is None:
        return None
    mdhd = _path(trak, b"mdia", b"mdhd")
    stbl = _path(trak, b"mdia", b"minf", b"stbl")
    if mdhd is None or stbl is None:
        return None
    d = mdhd.data
    if d[0] == 1:
        timescale, duration = struct.unpack_from(">IQ", d, 20)
    else:
        timescale, duration = struct.unpack_from(">II", d, 12)
    stsz = stbl.child(b"stsz")
    frames = struct.unpack_from(">I", stsz.data, 8)[0] if stsz is not None else 0
    stss = stbl.child(b"stss")
    keyframes = struct.unpack_from(">I", stss.data, 4)[0] if stss is not None else frames
    secs = duration / timescale if timescale else 0.0
    return {"frames": frames, "keyframes": keyframes, "duration": secs,
            "spacing": secs / keyframes if keyframes else None}


# ---------------------------------------------------------------- writing

def _full_box(type_, payload):
    return Box(type_, data=b"\0\0\0\0" + payload)


def _v1_box(stereo, software):
    xml = ('<?xml version="1.0"?><rdf:SphericalVideo xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
           'xmlns:GSpherical="http://ns.google.com/videos/1.0/spherical/">'
           "<GSpherical:Spherical>true</GSpherical:Spherical><GSpherical:Stitched>true</GSpherical:Stitched>"
           "<GSpherical:StitchingSoftware>%s</GSpherical:StitchingSoftware>"
           "<GSpherical:ProjectionType>equirectangular</GSpherical:ProjectionType>"
           "<GSpherical:StereoMode>%s</GSpherical:StereoMode></rdf:SphericalVideo>" % (software, stereo))
    return Box(b"uuid", data=V1_UUID + xml.encode("utf-8"))


def _v2_boxes(stereo, software):
    st3d = Box(b"st3d", data=b"\0\0\0\0" + bytes([STEREO_CODES.get(stereo, 0)]))
    svhd = _full_box(b"svhd", software.encode("utf-8") + b"\0")
    prhd = _full_box(b"prhd", struct.pack(">iii", 0, 0, 0))            # yaw, pitch, roll
    equi = _full_box(b"equi", struct.pack(">IIII", 0, 0, 0, 0))        # no cropping
    proj = Box(b"proj", children=[prhd, equi])
    return st3d, Box(b"sv3d", children=[svhd, proj])


def _shift_chunk_offsets(moov, delta):
    for trak in moov.children:
        if trak.type != b"trak":
            continue
        stbl = _path(trak, b"mdia", b"minf", b"stbl")
        if stbl is None:
            continue
        for c in stbl.children:
            if c.type == b"stco":
                n = struct.unpack_from(">I", c.data, 4)[0]
                offs = struct.unpack_from(">%dI" % n, c.data, 8)
                if offs and max(offs) + delta > 0xFFFFFFFF:
                    raise ValueError("chunk offsets would overflow 32 bits")
                c.data = c.data[:8] + struct.pack(">%dI" % n, *(o + delta for o in offs))
            elif c.type == b"co64":
                n = struct.unpack_from(">I", c.data, 4)[0]
                offs = struct.unpack_from(">%dQ" % n, c.data, 8)
                c.data = c.data[:8] + struct.pack(">%dQ" % n, *(o + delta for o in offs))


def inject_spherical(path, stereo="mono", software="360 Video Shrinker"):
    """Tag the video track of an MP4 as equirectangular 360 (V1 + V2). Replaces any existing tags."""
    moov, off, old_size, boxes, end = load_moov(path)
    trak = video_trak(moov)
    if trak is None:
        raise ValueError("no video track")
    se = sample_entry(trak)
    if se is None or se.children is None:
        raise ValueError("unsupported video sample entry")
    trak.children = [c for c in trak.children if not (c.type == b"uuid" and c.data[:16] == V1_UUID)]
    se.children = [c for c in se.children if c.type not in (b"st3d", b"sv3d")]
    st3d, sv3d = _v2_boxes(stereo, software)
    se.children += [st3d, sv3d]
    trak.children.append(_v1_box(stereo, software))
    mdat_after = any(t == b"mdat" and o > off for t, o, s in boxes)
    if not mdat_after:
        new = moov.serialize()
        with open(path, "r+b") as f:
            f.seek(off + old_size)
            tail = f.read()                     # boxes after moov (usually none, or a small 'free')
            f.seek(off)
            f.write(new)
            f.write(tail)
            f.truncate()
        return
    # moov in front of mdat: grow moov and move every chunk offset
    new = moov.serialize()
    delta = len(new) - old_size
    _shift_chunk_offsets(moov, delta)
    new = moov.serialize()
    tmp = path + ".meta.tmp"
    with open(path, "rb") as src, open(tmp, "wb") as dst:
        dst.write(src.read(off))
        dst.write(new)
        src.seek(off + old_size)
        shutil.copyfileobj(src, dst, 16 * 1024 * 1024)
    os.replace(tmp, path)
