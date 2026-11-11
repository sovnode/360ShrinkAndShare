"""Writes the release zip from the staged build folder so that it carries nothing about the PC that built it.

  python -I tools/make_zip.py BUILD_DIR OUT.zip

- every entry gets the same fixed date (no build times, install times or time zone), '/' separators,
  sorted order, plain file permissions and no extra fields;
- pip's own install records are dropped from every *.dist-info (INSTALLER, REQUESTED, direct_url.json),
  and RECORD keeps only the lines for files that are really shipped.
Called by package.ps1.
"""
import os
import sys
import zipfile

FIXED = (2026, 1, 1, 0, 0, 0)
PIP_WRITTEN = {"INSTALLER", "REQUESTED", "direct_url.json"}


def files(root):
    for dp, dns, fns in os.walk(root):
        dns.sort()
        for f in sorted(fns):
            p = os.path.join(dp, f)
            yield p, os.path.relpath(p, root).replace(os.sep, "/")


def record_filtered(path, distinfo_dir):
    """RECORD lines whose file exists in the staged site-packages (pip --target adds e.g. ../../bin/x.exe)."""
    site = os.path.dirname(distinfo_dir)
    keep = []
    with open(path, encoding="utf-8", newline="") as f:
        for line in f.read().splitlines():
            name = line.split(",", 1)[0]
            if not name:
                continue
            if os.path.basename(name) in PIP_WRITTEN:
                continue
            if os.path.exists(os.path.normpath(os.path.join(site, name))):
                keep.append(line)
    return ("\n".join(keep) + "\n").encode("utf-8")


def main(build, out):
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p, arc in files(build):
            parent = os.path.basename(os.path.dirname(p))
            if parent.endswith(".dist-info") and os.path.basename(p) in PIP_WRITTEN:
                continue
            if parent.endswith(".dist-info") and os.path.basename(p) == "RECORD":
                data = record_filtered(p, os.path.dirname(p))
            else:
                with open(p, "rb") as f:
                    data = f.read()
            zi = zipfile.ZipInfo(arc, date_time=FIXED)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.create_system = 0
            zi.external_attr = 0x20          # MS-DOS "archive" attribute only
            z.writestr(zi, data)
            n += 1
    print("zip entries:", n)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
