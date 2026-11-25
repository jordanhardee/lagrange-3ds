#!/usr/bin/env python3
"""Black-box Azahar acceptance test using a runner-local Gemini TLS fixture.

No guest hooks, direct browser commands, certificate bypasses or personal
accounts. All interactions go through the emulator's actual input path.
The fixture and screenshots are synthetic CI data, not user browsing data.
"""
import json
import os
from pathlib import Path
import socketserver
import ssl
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
from PIL import Image, ImageChops

OUT = Path(os.environ["OUT"])
DATA = Path(os.environ["XDG_DATA_HOME"]) / "azahar-emu" / "sdmc" / "3ds" / "lagrange"
PID = int(sys.argv[1])
BASE = "gemini://127.0.0.1:19650"
RESULT = OUT / "session-results.json"
REQUESTS = OUT / "gemini-requests.jsonl"
stages = {}
lock = threading.Lock()
requests = []


def run(*args, timeout=10):
    return subprocess.check_output(args, stderr=subprocess.STDOUT, timeout=timeout, text=True).strip()


def record(stage, status, detail=""):
    stages[stage] = {"status": status, "detail": detail}
    RESULT.write_text(json.dumps(stages, indent=2) + "\n")
    print(f"{stage}: {status}: {detail}", flush=True)


def alive():
    os.kill(PID, 0)


def wait_for(predicate, description, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        alive()
        result = predicate()
        if result:
            return result
        time.sleep(0.3)
    raise AssertionError(description)


class Capsule(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            with self.server.context.wrap_socket(self.request, server_side=True) as conn:
                conn.settimeout(8)
                request = b""
                while not request.endswith(b"\r\n") and len(request) <= 1024:
                    block = conn.recv(1024)
                    if not block:
                        return
                    request += block
                url = request.decode("utf-8").strip()
                path = urlsplit(url).path
                if path == "/start":
                    body = "# PORT ACCEPTANCE ALPHA\n\n=> /second Open the second page\n\n"
                    body += "\n".join(f"Paragraph {i:02d}: Circle Pad scrolling fixture." for i in range(1, 80))
                elif path == "/second":
                    body = "# PORT ACCEPTANCE BRAVO\n\nSecond page reached via controller navigation.\n"
                else:
                    conn.sendall(b"51 Not found\r\n")
                    return
                conn.sendall(("20 text/gemini; charset=utf-8\r\n" + body + "\n").encode())
                with lock:
                    event = {"url": url, "tls": conn.version(), "cipher": conn.cipher()[0], "status": 20}
                    requests.append(event)
                    with REQUESTS.open("a") as f:
                        f.write(json.dumps(event) + "\n")
        except (OSError, ssl.SSLError, UnicodeError) as exc:
            with lock, (OUT / "gemini-server-errors.log").open("a") as f:
                f.write(repr(exc) + "\n")


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def requested(path, after=0):
    with lock:
        return any(item["url"] == BASE + path and item["status"] == 20 for item in requests[after:])


def screenshot(name):
    target = OUT / f"{name}.png"
    run("import", "-window", "root", str(target))
    with Image.open(target) as img:
        return img.convert("RGB")


def locate_screens(img):
    # The pinned default Azahar layout renders native-sized 400x240 and
    # 320x240 surfaces stacked centrally. Check the actual white edges in
    # the first-frame image rather than blindly clicking desktop positions.
    center = img.width // 2
    for y in range(20, img.height - 480):
        def light(x, yy):
            return min(img.getpixel((x, yy))) > 200
        def dark(x, yy):
            return max(img.getpixel((x, yy))) < 50
        if (light(center - 199, y + 5) and light(center + 199, y + 5)
                and dark(center - 201, y + 5) and dark(center + 201, y + 5)
                and light(center - 199, y + 230) and light(center + 199, y + 230)
                and dark(center - 199, y + 250) and light(center - 159, y + 290)
                and light(center + 159, y + 290)
                and dark(center - 161, y + 290)):
            # Find the first row of the actual top screen.
            top = y + 5
            while top > 0 and light(center - 199, top - 1) and dark(center - 201, top - 1):
                top -= 1
            return (center - 200, top, center + 200, top + 240), (center - 160, top + 240)
    raise AssertionError("Could not locate native-size dual screens; inspect 01-startup.png")


def focus():
    return run("xdotool", "getwindowfocus")


def click(x, y):
    run("xdotool", "mousemove", str(x), str(y), "mousedown", "1")
    time.sleep(0.15)
    run("xdotool", "mouseup", "1")


def press(key, duration=0.15):
    run("xdotool", "keydown", key)
    time.sleep(duration)
    run("xdotool", "keyup", key)


def open_keyboard(bottom, main_window):
    click(bottom[0] + 100, bottom[1] + 23)
    dialog = wait_for(lambda: (w if (w := focus()) != main_window else None),
                      "Touching the address bar did not open the native keyboard", 12)
    return dialog


def difference(a, b):
    # Count visibly changed pixels, ignoring tiny antialiasing differences.
    diff = ImageChops.difference(a, b).convert("L")
    return sum(diff.histogram()[24:]) / (a.width * a.height)


def changed_page(top, previous, name, minimum=0.015):
    def check():
        current = screenshot(name).crop(top)
        return current if difference(previous, current) >= minimum else None
    current = wait_for(check, f"No significant upper-screen change for {name}", 20)
    time.sleep(1.0)
    return screenshot(name).crop(top)


def restored_page(top, reference, name):
    def check():
        img = screenshot(name).crop(top)
        # The small selected-link highlight and pointer can differ.
        return img if difference(reference, img) < 0.025 else None
    return wait_for(check, f"Upper screen did not return to expected page for {name}", 15)


def session():
    current = "layout"
    for stage in ("layout", "keyboard_cancel", "keyboard_submit", "gemini_tls", "page_render",
                  "circle_pad", "dpad_link", "back", "forward", "history_write"):
        record(stage, "pending")
    try:
        time.sleep(4)
        initial = screenshot("01-startup")
        top, bottom = locate_screens(initial)
        main_window = focus()
        record(current, "pass", f"top={top}, bottom={bottom}, focus={main_window}")
        current = "keyboard_cancel"
        open_keyboard(bottom, main_window)
        screenshot("02-keyboard-cancel")
        run("xdotool", "key", "ctrl+a")
        run("xdotool", "type", "--clearmodifiers", BASE + "/cancelled")
        press("Escape")
        wait_for(lambda: focus() == main_window, "Keyboard did not close after cancel", 10)
        time.sleep(1)
        if requests:
            raise AssertionError("Cancelled address unexpectedly reached the Gemini fixture")
        record(current, "pass", "Keyboard closed; fixture received no request")

        current = "keyboard_submit"
        open_keyboard(bottom, main_window)
        run("xdotool", "key", "ctrl+a")
        run("xdotool", "type", "--clearmodifiers", BASE + "/start")
        screenshot("03-keyboard-address")
        press("Return")
        wait_for(lambda: focus() == main_window, "Keyboard did not return to browser", 10)
        record(current, "pass", "Address submitted through the native software keyboard")

        current = "gemini_tls"
        wait_for(lambda: requested("/start"), "Fixture received no successful /start Gemini request", 25)
        record(current, "pass", "Runner-local TLS server received exact URL and sent status 20")

        current = "page_render"
        alpha = changed_page(top, initial.crop(top), "04-alpha")
        record(current, "pass", "Upper framebuffer changed after successful Gemini response; inspect screenshot for text")

        current = "circle_pad"
        # Azahar defaults: Arrow keys = Circle Pad, T/G/F/H = D-pad.
        press("Down", 1.2)
        scrolled = changed_page(top, alpha, "05-scrolled")
        press("Up", 2.0)
        restored_page(top, alpha, "06-scroll-restored")
        record(current, "pass", f"Scroll moved {difference(alpha, scrolled):.1%} of upper-screen pixels and returned")

        current = "dpad_link"
        press("g")
        screenshot("07-link-selected")
        press("a")
        wait_for(lambda: requested("/second"), "D-pad Down + A did not request the second page", 25)
        bravo = changed_page(top, alpha, "08-bravo")
        record(current, "pass", "Controller selected and opened the fixture's relative link")

        current = "back"
        press("s")  # physical B
        restored_page(top, alpha, "09-back")
        record(current, "pass", "Physical B restored first page")

        current = "forward"
        click(bottom[0] + 96, bottom[1] + 218)
        restored_page(top, bravo, "10-forward")
        record(current, "pass", "Touchscreen forward arrow restored second page")

        current = "history_write"
        history = DATA / "visited.lst"
        def history_saved():
            try:
                text = history.read_text()
                return text if BASE + "/start" in text and BASE + "/second" in text else None
            except (OSError, UnicodeError):
                return None
        saved = wait_for(history_saved, "Both fixture URLs not found in visited.lst", 45)
        (OUT / "visited-fixture.lst").write_text(saved)
        record(current, "pass", "Both URLs saved by browser to virtual SD; relaunch verification is separate")
    except Exception as exc:
        record(current, "fail", str(exc))
        screenshot("99-failure")
        for stage, result in list(stages.items()):
            if result["status"] == "pending":
                record(stage, "blocked", f"Prerequisite {current} failed")
        raise


def main():
    # Key/cert remain in a temporary directory and are never uploaded.
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        cert = Path(tmp) / "cert.pem"
        key = Path(tmp) / "key.pem"
        run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
            "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
            "-keyout", str(key), "-out", str(cert), timeout=20)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert, key)
        with Server(("127.0.0.1", 19650), Capsule) as server:
            server.context = context
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                session()
            finally:
                server.shutdown()
                thread.join(timeout=3)


if __name__ == "__main__":
    main()
