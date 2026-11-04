"""shot, record, logcat."""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

from .. import store, transfer, ui
from ..adb import AdbError, check_pkg, popen
from ..ui import c
from . import add


def register(sub, common):
    p = add(sub, common, "shot", "Screenshot of the headset to the PC", cmd_shot, aliases=["screenshot"])
    p.add_argument("file", nargs="?", help="Target file (default: ~/Quest/screenshots/…png)")
    p.add_argument("-o", "--open", action="store_true", help="open it afterwards")
    p.add_argument("-w", "--wake", action="store_true", help="briefly wake a sleeping headset for it")
    p.add_argument("-m", "--meta", action="store_true",
                   help="System screenshot (one eye, as in the menu) instead of the raw image of both panels")
    p = add(sub, common, "record", "Record the screen (Ctrl+C stops) and copy it to the PC", cmd_record,
            aliases=["rec"])
    p.add_argument("file", nargs="?")
    p.add_argument("-t", "--time", type=int, default=180, help="Maximum duration in s (<= 180)")
    p.add_argument("-b", "--bitrate", default="20M", help="e.g. 8M, 20M")
    p.add_argument("--size", help="e.g. 1920x1080")
    p = add(sub, common, "logcat", "Live log, filtered and colorized", cmd_logcat, aliases=["log"])
    p.add_argument("-p", "--package", help="only logs from this app")
    p.add_argument("-l", "--level", choices=list("VDIWEF"), default="V", help="Minimum level")
    p.add_argument("-g", "--grep", help="Regex filter")
    p.add_argument("-t", "--tag", action="append", help="only these tags (repeatable)")
    p.add_argument("-d", "--dump", action="store_true", help="print the existing log and exit")
    p.add_argument("--crash", action="store_true", help="crash buffer only")
    p.add_argument("--save", help="also write to a file")
    p.add_argument("--clear", action="store_true", help="clear the log first")


def _ts() -> str:
    return time.strftime("%Y-%m-%d_%H-%M-%S")


def _awake(dev) -> bool:
    return "mWakefulness=Awake" in dev.sh("dumpsys power | grep -m1 mWakefulness").out


def _meta_shot(dev, dest: Path) -> Path:
    """System screenshot via com.oculus.metacam (one eye, undistorted - as in the menu)."""
    folder = "/sdcard/Oculus/Screenshots"
    before = set(dev.sh(f"ls {folder} 2>/dev/null").text.split())
    r = dev.sh("am startservice -n com.oculus.metacam/.capture.CaptureService -a TAKE_SCREENSHOT")
    if "Error" in r.out:
        raise AdbError("metacam screenshot not possible.", r.out + "\nIs com.oculus.metacam disabled?")
    for _ in range(20):
        time.sleep(0.5)
        new = set(dev.sh(f"ls {folder} 2>/dev/null").text.split()) - before
        if new:
            name = sorted(new)[-1]
            time.sleep(0.5)
            if dest.suffix.lower() == ".png":
                dest = dest.with_suffix(".jpg")
            return transfer.pull(dev, f"{folder}/{name}", dest, name=name)
    raise AdbError("metacam did not save an image.")


def cmd_shot(ctx):
    dev = ctx.dev
    dest = Path(ctx.args.file) if ctx.args.file else transfer.default_dir("screenshots") / f"quest_{_ts()}.png"
    dest.parent.mkdir(parents=True, exist_ok=True)
    woke = False
    if not _awake(dev):
        if not ctx.args.wake:
            raise AdbError("The headset is asleep (display off) - no image is possible.",
                           "Put it on, or without putting it on: questtools shot --wake")
        dev.sh("am broadcast -a com.oculus.vrpowermanager.prox_close; input keyevent KEYCODE_WAKEUP")
        woke = True
        with ui.Spinner("Waking headset …"):
            for _ in range(16):
                time.sleep(0.5)
                if _awake(dev):
                    break
            time.sleep(5)  # the compositor needs a moment before it produces an image again
    try:
        if ctx.args.meta:
            dest = _meta_shot(dev, dest)
            data_len = dest.stat().st_size
        else:
            with ui.Spinner("Screenshot …"):
                r = dev.run("exec-out", "screencap", "-p", timeout=30, capture_bytes=True)
            data = r.out
            if not data or not data.startswith(b"\x89PNG"):  # type: ignore[union-attr]
                raise AdbError("Screenshot failed.", r.err or "screencap returned no image.")
            dest.write_bytes(data)  # type: ignore[arg-type]
            data_len = len(data)
    finally:
        if woke:
            dev.sh("am broadcast -a com.oculus.vrpowermanager.automation_disable")
    if ctx.emit({"file": str(dest), "bytes": data_len}):
        return
    ui.ok(f"Saved: {dest}  {c(ui.human_bytes(data_len), 'grey')}")
    if ctx.args.open:
        import os
        os.startfile(dest) if hasattr(os, "startfile") else None


def cmd_record(ctx):
    dev = ctx.dev
    if not _awake(dev):
        raise AdbError("The headset is asleep (display off) - the recording would be black.",
                       "Put it on, or keep the display awake: questtools quest proximity off && questtools quest key wake")
    t = max(1, min(180, ctx.args.time))
    remote = f"/sdcard/qt_rec_{int(time.time())}.mp4"
    dest = Path(ctx.args.file) if ctx.args.file else transfer.default_dir("videos") / f"quest_{_ts()}.mp4"
    bitrate = ctx.args.bitrate.upper().replace("M", "000000").replace("K", "000")
    if not bitrate.isdigit():
        raise AdbError("Specify a bitrate like 8M or 20M.")
    args = ["-s", dev.serial, "shell", "screenrecord", "--time-limit", str(t), "--bit-rate", bitrate]
    if ctx.args.size:
        if not re.fullmatch(r"\d+x\d+", ctx.args.size):
            raise AdbError("Specify a size like 1920x1080.")
        args += ["--size", ctx.args.size]
    args.append(remote)
    proc = popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    t0 = time.time()
    ui.info(f"Recording (max. {t}s) - {c('Ctrl+C', 'bold')} stops.")
    try:
        while proc.poll() is None:
            el = time.time() - t0
            if not ui.JSON_MODE:
                sys.stdout.write(f"\r  {c('●', 'red')} REC {int(el // 60):02d}:{int(el % 60):02d}  "
                                 f"{ui.bar(el / t, 30, 2, 2)}")
                sys.stdout.flush()
            time.sleep(0.25)
    except KeyboardInterrupt:
        dev.sh("pkill -INT screenrecord")
        proc.wait(timeout=10)
    print()
    time.sleep(1.0)  # let the MP4 container finish writing
    if proc.returncode not in (0, None, 130, 255, 1):
        err = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
        raise AdbError("screenrecord failed.", err)
    transfer.pull(dev, remote, dest, name=dest.name)
    dev.sh(f"rm -f {remote}")
    ui.ok(f"Video saved: {dest}")


LEVEL_STYLE = {"V": "grey", "D": "blue", "I": "green", "W": "yellow", "E": "red", "F": "magenta"}
LOG_RE = re.compile(r"^(\d\d-\d\d) (\d\d:\d\d:\d\d\.\d+)\s+(\d+)\s+(\d+)\s+([VDIWEF])\s+(.*?)\s*:\s(.*)$")


def cmd_logcat(ctx):
    a = ctx.args
    dev = ctx.dev
    if a.clear:
        dev.run("logcat", "-c")
        ui.ok("Log cleared.")
    args = ["-s", dev.serial, "logcat", "-v", "threadtime"]
    if a.dump:
        args.append("-d")
    if a.crash:
        args += ["-b", "crash"]
    if a.package:
        pid = dev.sh(f"pidof {check_pkg(a.package)}").text.split()
        if not pid:
            raise AdbError(f"{a.package} is not running.", f"Start it: questtools apps launch {a.package}")
        args.append(f"--pid={pid[0]}")
    if a.tag:
        args += [f"{t}:{a.level}" for t in a.tag] + ["*:S"]
    else:
        args.append(f"*:{a.level}")
    try:
        rx = re.compile(a.grep, re.I) if a.grep else None
    except re.error as e:
        raise AdbError(f"Invalid search pattern: {e}")
    save = open(a.save, "a", encoding="utf-8") if a.save else None
    proc = popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    ui.info(f"logcat {dev.serial} · Level ≥ {a.level}" + (f" · {a.package}" if a.package else "")
            + ("" if a.dump else c("  (Ctrl+C stops)", "grey")))
    try:
        assert proc.stdout
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            if rx and not rx.search(line):
                continue
            if save:
                save.write(line + "\n")
            m = LOG_RE.match(line)
            if not m:
                print(c(line, "grey"))
                continue
            _, tm, pid, _, lvl, tag, msg = m.groups()
            st = LEVEL_STYLE[lvl]
            print(f"{c(tm[:12], 'grey')} {c(lvl, st, 'bold')} {c(ui.pad(ui.trunc(tag, 22), 22), 'cyan')} "
                  f"{c(msg, st) if lvl in 'EWF' else msg}")
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()
        if save:
            save.close()
            ui.hint(f"saved to {a.save}")
