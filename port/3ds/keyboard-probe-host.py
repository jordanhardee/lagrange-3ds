#!/usr/bin/env python3
"""Compare native libctru keyboard behavior with browser behavior in Azahar."""
import os
import subprocess
import time
from pathlib import Path
from PIL import Image

out = Path(os.environ["OUT"])
data = Path(os.environ["XDG_DATA_HOME"])
data.mkdir(parents=True, exist_ok=True)
env = dict(os.environ, LD_LIBRARY_PATH=os.environ["AZAHAR_LIBRARY_PATH"],
           LIBGL_ALWAYS_SOFTWARE="1", QT_QPA_PLATFORM="xcb")
log = data / "azahar-emu/log/azahar_log.txt"

def wait_for(check, label, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.25)
    raise RuntimeError(label)

def logs():
    try:
        return log.read_text(errors="replace")
    except OSError:
        return ""

def screenshot(name):
    path = out / (name + ".png")
    subprocess.run(["import", "-window", "root", str(path)], check=True, timeout=5)
    with Image.open(path) as image:
        return image.convert("RGB")

def dialog(name):
    image = screenshot(name)
    cx = image.width // 2
    for y in range(image.height // 2 - 180, image.height // 2 + 100):
        def gray(x, yy):
            r, g, b = image.getpixel((x, yy))
            return 220 <= r <= 249 and abs(r-g) <= 3 and abs(r-b) <= 3
        def dark(x, yy):
            return max(image.getpixel((x, yy))) < 40
        if not gray(cx, y) or not dark(cx, y-1):
            continue
        left = right = cx
        while left > cx - 350 and gray(left-1, y):
            left -= 1
        while right < cx + 350 and gray(right, y):
            right += 1
        if not 180 <= right-left <= 350:
            continue
        bottom = y
        while bottom < y+160 and gray(left+2, bottom):
            bottom += 1
        if 75 <= bottom-y <= 150 and dark(left-1, y+5) and dark(right, y+5):
            return left, y, right, bottom

def click(x, y):
    subprocess.run(["xdotool", "mousemove", str(x), str(y), "click", "1"], check=True, timeout=5)

status = "failed"
with (out / "console.log").open("w") as console:
    proc = subprocess.Popen([os.environ["EMULATOR"], os.environ["KEYBOARD_PROBE"]],
                            env=env, stdout=console, stderr=subprocess.STDOUT)
    try:
        for kind in ("CANCEL", "OK"):
            wait_for(lambda: "KEYBOARD_PROBE_OPEN_" + kind in logs(), "Guest did not open " + kind, 30)
            x1,y1,x2,y2 = wait_for(lambda: dialog(kind.lower()+"-dialog"), "Keyboard dialog not found")
            if kind == "OK":
                click((x1+x2)//2, y2-52)
                subprocess.run(["xdotool","key","ctrl+a"], check=True, timeout=5)
                subprocess.run(["xdotool","type","--clearmodifiers","probe-ok"], check=True, timeout=5)
            click(x1+round((x2-x1)*(0.745 if kind=="CANCEL" else 0.315)), y2-23)
            wait_for(lambda: "KEYBOARD_PROBE_"+kind+"_PASS" in logs(), "Guest did not return successfully from "+kind)
        status = "passed"
    except Exception as exc:
        print("Independent keyboard probe:", exc, flush=True)
        (out / "failure.txt").write_text(str(exc))
    finally:
        try:
            screenshot("final")
        except Exception:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)
        (out / "azahar_log.txt").write_text(logs())
        (out / "result.txt").write_text(status+"\n")
        print("Independent keyboard probe:", status, flush=True)
raise SystemExit(0 if status=="passed" else 1)
