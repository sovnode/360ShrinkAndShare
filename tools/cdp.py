"""Drive a headless Edge or Chrome for tests (Chrome DevTools Protocol over a small built-in WebSocket client;
standard library only). Used by the browser tests of the HTML exports: the page is opened from disk (file://),
exactly as a colleague would open it.

    with cdp.Browser() as b:
        b.open(path_to_html)
        print(b.eval("document.title"))
        b.screenshot(png_path)
"""
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.request

BROWSERS = (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe")


def find_browser(prefer=None):
    for p in ([prefer] if prefer else []) + list(BROWSERS):
        if p and os.path.exists(p):
            return p
    return None


class _WS:
    def __init__(self, url):
        rest = url.split("://", 1)[1]
        hostport, path = rest.split("/", 1)
        host, port = hostport.split(":")
        self.s = socket.create_connection((host, int(port)), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall(("GET /%s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                        "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (path, hostport, key)).encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.s.recv(4096)
        if b" 101 " not in buf.split(b"\r\n", 1)[0]:
            raise RuntimeError("WebSocket handshake failed: %r" % buf[:80])
        self.buf = buf.split(b"\r\n\r\n", 1)[1]

    def send(self, text):
        data = text.encode("utf-8")
        mask = os.urandom(4)
        n = len(data)
        head = b"\x81" + (struct.pack("!B", 0x80 | n) if n < 126 else
                          struct.pack("!BH", 0x80 | 126, n) if n < 65536 else struct.pack("!BQ", 0x80 | 127, n))
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(data)) if n < 4096 else \
            (int.from_bytes(data, "big") ^ int.from_bytes((mask * (n // 4 + 1))[:n], "big")).to_bytes(n, "big")
        self.s.sendall(head + mask + body)

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.s.recv(1 << 20)
            if not chunk:
                raise ConnectionError("browser closed the connection")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self):
        msg = b""
        while True:
            b1, b2 = self._read(2)
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read(8))[0]
            data = self._read(n)
            op = b1 & 0x0F
            if op == 8:
                raise ConnectionError("browser closed the connection")
            if op in (9, 10):
                continue
            msg += data
            if b1 & 0x80:
                return msg.decode("utf-8", "replace")

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass


class Browser:
    def __init__(self, exe=None, width=1280, height=720, headless=True, port=None, profile_parent=None):
        self.exe = find_browser(exe)
        if not self.exe:
            raise RuntimeError("no Edge or Chrome found")
        self.port = port or _free_port()
        self.profile = tempfile.mkdtemp(prefix="browser-test-", dir=profile_parent)
        args = [self.exe, "--remote-debugging-port=%d" % self.port, "--user-data-dir=" + self.profile,
                "--no-first-run", "--no-default-browser-check", "--disable-extensions",
                "--autoplay-policy=no-user-gesture-required", "--window-size=%d,%d" % (width, height),
                "--remote-allow-origins=*", "about:blank"]
        if headless:
            args[1:1] = ["--headless=new", "--enable-gpu", "--ignore-gpu-blocklist"]
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.ws = None
        self.id = 0
        self.events = []
        deadline = time.time() + 30
        while True:
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/json" % self.port, timeout=2) as r:
                    pages = [t for t in json.load(r) if t.get("type") == "page"]
                if pages:
                    self.ws = _WS(pages[0]["webSocketDebuggerUrl"])
                    break
            except OSError:
                pass
            if time.time() > deadline:
                self.close()
                raise RuntimeError("the browser didn't start")
            time.sleep(0.3)
        self.call("Page.enable")
        self.call("Runtime.enable")

    def call(self, method, **params):
        self.id += 1
        mid = self.id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError("%s: %s" % (method, msg["error"]))
                return msg.get("result", {})
            self.events.append(msg)

    def open(self, path_or_url, wait=1.0):
        url = path_or_url if "://" in path_or_url else "file:///" + urllib.request.pathname2url(
            os.path.abspath(path_or_url)).lstrip("/")
        self.call("Page.navigate", url=url)
        time.sleep(wait)
        return url

    def eval(self, expr, timeout=60):
        r = self.call("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True,
                      timeout=int(timeout * 1000))
        if "exceptionDetails" in r:
            d = r["exceptionDetails"]
            raise RuntimeError("page error: %s" % (d.get("exception", {}).get("description") or d.get("text")))
        return r.get("result", {}).get("value")

    def wait_for(self, expr, timeout=60, step=0.25):
        end = time.time() + timeout
        while time.time() < end:
            v = self.eval(expr)
            if v:
                return v
            time.sleep(step)
        raise TimeoutError("waited %s s for: %s" % (timeout, expr))

    def key(self, key, code=None, vk=0):
        for typ in ("keyDown", "keyUp"):
            self.call("Input.dispatchKeyEvent", type=typ, key=key, code=code or key, windowsVirtualKeyCode=vk,
                      nativeVirtualKeyCode=vk)

    def drag(self, x0, y0, x1, y1, steps=6):
        self.call("Input.dispatchMouseEvent", type="mousePressed", x=x0, y=y0, button="left", buttons=1, clickCount=1)
        for i in range(1, steps + 1):
            self.call("Input.dispatchMouseEvent", type="mouseMoved", x=x0 + (x1 - x0) * i / steps,
                      y=y0 + (y1 - y0) * i / steps, button="left", buttons=1)
        self.call("Input.dispatchMouseEvent", type="mouseReleased", x=x1, y=y1, button="left", buttons=0, clickCount=1)

    def click(self, x, y):
        self.drag(x, y, x, y, steps=1)

    def wheel(self, x, y, dy):
        self.call("Input.dispatchMouseEvent", type="mouseWheel", x=x, y=y, deltaX=0, deltaY=dy)

    def screenshot(self, path):
        data = self.call("Page.captureScreenshot", format="png")["data"]
        with open(path, "wb") as f:
            f.write(base64.b64decode(data))
        return path

    def console(self):
        out = []
        for e in self.events:
            if e.get("method") == "Runtime.consoleAPICalled":
                out.append(" ".join(str(a.get("value", a.get("description", ""))) for a in e["params"]["args"]))
            elif e.get("method") == "Runtime.exceptionThrown":
                d = e["params"]["exceptionDetails"]
                out.append("EXCEPTION " + str(d.get("exception", {}).get("description") or d.get("text")))
        return out

    def close(self):
        try:
            if self.ws:
                try:
                    self.call("Browser.close")
                except Exception:  # noqa: BLE001
                    pass
                self.ws.close()
        finally:
            try:
                self.proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                self.proc.kill()
            for _ in range(10):
                shutil.rmtree(self.profile, ignore_errors=True)
                if not os.path.exists(self.profile):
                    break
                time.sleep(0.5)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port
