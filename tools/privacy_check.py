"""Privacy check before publishing: scans what git would publish (and optionally a release zip) for
personal or machine-identifying text. Exit code 1 if anything is found.

  python tools/privacy_check.py                         the files git tracks (what a push publishes)
  python tools/privacy_check.py --zip FILE.zip          also every file inside a release zip
  python tools/privacy_check.py --history               also every commit's author, committer, date and message
  python tools/privacy_check.py --zip F --write-allow   record the zip's current hits as reviewed (see below)

Looks for:
- the terms in private/denylist.txt (one per line, case-insensitive; '\\' and '/' count as the same, and a term
  with spaces also matches with '', '_', '-' or '%20' instead) - that folder is never committed,
- this PC's user name, computer name and domain (read from the environment at run time, never stored),
- e-mail addresses, Windows user-profile paths (C:\\Users\\<name>), other absolute drive paths, UNC paths and
  IP addresses in text files; e-mail addresses, user-profile and UNC paths in binaries too,
- files that must never be published (logs, settings, install.json, shortcuts, photos, videos, private notes),
- commit times that are not in UTC (they give away the time zone).
Searches ASCII/UTF-8 and UTF-16 (LE and BE).

Bundled third-party files (Python, numpy, Pillow, PyAV) contain upstream e-mails, test IP addresses and common
words. After reviewing such hits once, --write-allow stores them in private/allow.txt keyed by file path, hit and
the file's SHA-256, so they are not reported again unless the file changes. Hits in the program's own files
(payload/app, setup.py, Install.cmd, the read-me files) can never be allowed this way.
Runs as the git pre-push hook (tools/install_hooks.py).
"""
import argparse
import hashlib
import io
import os
import re
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRIVATE = os.path.join(ROOT, "private")
DENYLIST = os.path.join(PRIVATE, "denylist.txt")
ALLOWFILE = os.path.join(PRIVATE, "allow.txt")
ALLOW_EMAIL = re.compile(r"(@users\.noreply\.github\.com|^noreply@anthropic\.com|@example\.(com|org|net))$", re.I)
EMAIL = re.compile(rb"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
USERPATH = re.compile(rb"[A-Za-z]:[\\/]+(?:Users|Documents and Settings)[\\/]+(?!Public\b|Default\b|<|%|\{)[^\\/\s\"'<>]+",
                      re.I)
DRIVEPATH = re.compile(rb"(?<![A-Za-z0-9])[A-Za-z]:[\\/]+[A-Za-z0-9 _.()-]{2,}")
GENERIC_DRIVEPATH = re.compile(r"[A-Za-z]:[\\/]+(Program Files|Windows|ProgramData)\b|[A-Za-z]:[\\/]+Users$", re.I)
UNC = re.compile(rb"(?<![\\A-Za-z0-9])\\\\[A-Za-z0-9.-]{3,}\\[A-Za-z0-9$ _.-]+")
IPV4 = re.compile(rb"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\d.])")
IGNORE_IP = {b"0.0.0.0", b"127.0.0.1", b"255.255.255.255"}
NEVER = re.compile(r"(^|/)(install\.json|presets\.json|settings\.json|install-record\.json|direct_url\.json|"
                   r"denylist\.txt|allow\.txt|pyvenv\.cfg|(site|user)customize\.py|[^/]*\.(jsonl|lnk|log|pth|pyc)|"
                   r"[^/]*\.private\.[^/]*|private/.*)$", re.I)
MEDIA = re.compile(r"\.(jpe?g|png|gif|heic|heif|webp|dng|tiff?|mp4|mov|m4v|mkv|webm|avi|insv|insp|360|lrv|msg|zip|7z)$", re.I)
TEXT_EXT = {".py", ".pyw", ".ps1", ".cmd", ".bat", ".txt", ".md", ".json", ".pth", ".cfg", ".toml", ".ini", ".yml",
            ".yaml", ".html", ".xml", ".csv", ".tcl", ".sh", ".gitignore", ""}
# the program's own files inside a release zip: hits there are never allowed
OWN = re.compile(r"^[^/]+/(payload/app/|payload/setup\.py$|[^/]+$)")
BUNDLED = re.compile(r"^[^/]+/payload/python/")


def norm(b):
    """lower case, every run of '\\' or '/' becomes one '/'."""
    return re.sub(rb"[\\/]+", b"/", b.lower())


def terms():
    out = []
    if os.path.exists(DENYLIST):
        for line in io.open(DENYLIST, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line)
    for var in ("USERNAME", "COMPUTERNAME", "USERDOMAIN", "USERDNSDOMAIN"):
        v = os.environ.get(var, "")
        if len(v) >= 3 and v.upper() != "WORKGROUP":
            out.append(v)
    variants = []
    for t in out:
        forms = {t}
        if " " in t:
            forms |= {t.replace(" ", r) for r in ("", "_", "-", ".", "+", "%20", "  ", "\n", "\r\n", "\n# ")}
        variants.append((t, sorted(forms)))
    return variants


def scan_bytes(name, data, words, binary=False):
    hits = []
    views = {}
    for enc in ("utf-8", "utf-16-le", "utf-16-be"):
        views[enc] = norm(data) if enc == "utf-8" else None
    for t, forms in words:
        found = None
        for f in forms:
            for enc in ("utf-8", "utf-16-le", "utf-16-be"):
                needle = norm(f.encode(enc)) if enc == "utf-8" else f.lower().encode(enc)
                hay = views[enc] if enc == "utf-8" else data.lower()
                if enc != "utf-8" and ("\\" in f or "/" in f):
                    for sep in ("\\", "/"):
                        n2 = f.lower().replace("\\", sep).replace("/", sep).encode(enc)
                        if n2 in hay:
                            found = (f, enc)
                            break
                elif needle in hay:
                    found = (f, enc)
                if found:
                    break
            if found:
                break
        if found:
            hits.append((name, "term:" + t, "private term %r (as %r, %s)" % (t, found[0], found[1])))
    for m in EMAIL.finditer(data):
        s = m.group().decode("latin-1")
        if not ALLOW_EMAIL.search(s):
            hits.append((name, "email:" + s.lower(), "e-mail address %r" % s))
    for rx, what in ((USERPATH, "user-profile path"), (UNC, "network path")):
        for m in rx.finditer(data):
            s = m.group().decode("latin-1")
            hits.append((name, what + ":" + s.lower(), "%s %r" % (what, s)))
    if not binary:
        for m in DRIVEPATH.finditer(data):
            s = m.group().decode("latin-1")
            if not GENERIC_DRIVEPATH.match(s):
                hits.append((name, "path:" + s.lower(), "absolute path %r (check it is generic)" % s))
        for m in IPV4.finditer(data):
            if m.group() not in IGNORE_IP:
                s = m.group().decode()
                hits.append((name, "ip:" + s, "IP address %r (check it is not a real one)" % s))
    return hits


def is_text(name):
    base = name.rsplit("/", 1)[-1]
    ext = os.path.splitext(base)[1].lower()
    return ext in TEXT_EXT or base.lower() in ("license", "record", "metadata", "wheel", "readme")


def tracked():
    out = subprocess.run(["git", "-C", ROOT, "ls-files", "-z", "--cached"], capture_output=True, check=True).stdout
    return [p for p in out.decode("utf-8").split("\0") if p]


def git_blob(spec):
    p = subprocess.run(["git", "-C", ROOT, "cat-file", "blob", spec], capture_output=True)
    return p.stdout if p.returncode == 0 else None


def read_file(p):
    try:
        with open(p, "rb") as f:
            return f.read()
    except OSError:
        return None


def load_allow():
    allow = set()
    if os.path.exists(ALLOWFILE):
        for line in io.open(ALLOWFILE, encoding="utf-8"):
            parts = line.rstrip("\n").split("\t")
            if len(parts) == 3:
                allow.add(tuple(parts))
    return allow


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", action="append", default=[])
    ap.add_argument("--history", action="store_true")
    ap.add_argument("--write-allow", action="store_true")
    a = ap.parse_args()
    words = terms()
    problems = []
    for rel in tracked():
        r = rel.replace("\\", "/")
        if NEVER.search(r) or MEDIA.search(r):
            problems.append("%s: this kind of file must not be published" % rel)
        # the staged copy (what a commit publishes), and the working copy if it differs
        seen = set()
        for label, data in (("staged", git_blob(":" + rel)), ("working copy", read_file(os.path.join(ROOT, rel)))):
            if data is None or data in seen:
                continue
            seen.add(data)
            problems += ["%s (%s): %s" % (h[0], label, h[2]) for h in scan_bytes(rel, data, words, not is_text(r))]
        problems += ["%s: %s" % (h[0], h[2]) for h in scan_bytes("(file name) " + rel, rel.encode("utf-8"), words)]
    allow, new_allow, allowed = load_allow(), set(), 0
    for z in a.zip:
        with zipfile.ZipFile(z) as zf:
            if zf.comment:
                problems += ["%s (zip comment): %s" % (z, h[2]) for h in scan_bytes("comment", zf.comment, words)]
            for info in zf.infolist():
                fn = info.filename
                for _, key, msg in scan_bytes(fn, fn.encode("utf-8"), words, True):
                    k = (fn, "name " + key, "-")
                    if BUNDLED.match(fn) and k in allow:
                        allowed += 1
                    elif a.write_allow and BUNDLED.match(fn):
                        new_allow.add(k)
                    else:
                        problems.append("(zip entry name) %s: %s" % (fn, msg))
                if "\\" in fn or fn.startswith("/") or ".." in fn.split("/"):
                    problems.append("%s!%s: bad entry name" % (z, fn))
                if NEVER.search(fn) or (MEDIA.search(fn) and not BUNDLED.match(fn)):
                    problems.append("%s!%s: this kind of file must not be published" % (z, fn))
                if info.is_dir():
                    continue
                data = zf.read(info)
                sha = hashlib.sha256(data).hexdigest()
                for _, key, msg in scan_bytes(fn, data, words, not is_text(fn)):
                    k = (fn, key, sha)
                    if not OWN.match(fn) and k in allow:
                        allowed += 1
                        continue
                    if a.write_allow and not OWN.match(fn):
                        new_allow.add(k)
                        continue
                    problems.append("%s!%s: %s" % (os.path.basename(z), fn, msg))
    if a.write_allow:
        with io.open(ALLOWFILE, "a", encoding="utf-8") as f:
            for k in sorted(new_allow - allow):
                f.write("\t".join(k) + "\n")
        print("%d reviewed third-party hits added to %s" % (len(new_allow - allow), ALLOWFILE))
    if a.history:
        log = subprocess.run(["git", "-C", ROOT, "log", "--all", "--format=%an%n%ae%n%cn%n%ce%n%ai%n%ci%n%B"],
                             capture_output=True, check=True).stdout
        problems += ["(git history): %s" % h[2] for h in scan_bytes("(git history)", log, words)]
        for line in log.decode("utf-8", "replace").splitlines():
            if re.match(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d [+-]\d{4}$", line) and not line.endswith(" 00:00:00 +0000"):
                problems.append("(git history): commit time %s is not a plain UTC date (it gives away the time zone "
                                "or working hours; commit with tools/commit.py)" % line)
        # every file version in every commit, including ones a later commit removed
        blobs = subprocess.run(["git", "-C", ROOT, "rev-list", "--all", "--objects"], capture_output=True,
                               check=True).stdout.decode("utf-8", "replace").splitlines()
        for line in blobs:
            sha, _, path = line.partition(" ")
            if not path or subprocess.run(["git", "-C", ROOT, "cat-file", "-t", sha], capture_output=True,
                                          text=True).stdout.strip() != "blob":
                continue
            r = path.replace("\\", "/")
            if NEVER.search(r) or MEDIA.search(r):
                problems.append("(git history) %s: this kind of file must not be published" % path)
            problems += ["(git history) %s @%s: %s" % (path, sha[:8], h[2])
                         for h in scan_bytes(path, git_blob(sha) or b"", words, not is_text(r))]
    for p in problems:
        print(p)
    if allowed:
        print("(%d reviewed third-party hits not shown: private/allow.txt)" % allowed)
    print("privacy check: %s" % ("%d problem(s)" % len(problems) if problems else "clean"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
