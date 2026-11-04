"""mirror: live view of the headset on the PC via scrcpy (optional external tool).

The Quest panel shows both eyes side by side (Quest 3: 4128x2208), so a plain mirror
is a wide stereo image. By default one eye is cropped out, which looks like a normal
screen. While mirroring, the proximity sensor is overridden so the display keeps
running without wearing the headset; it is restored when the window closes.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from .. import store, transfer, ui
from ..adb import AdbError, find_adb
from ..ui import c
from . import add, dry

INSTALL_HINT = ("Let Quest Tools install it: questtools mirror --install-scrcpy\n"
                "or install it yourself (winget install Genymobile.scrcpy · scoop install scrcpy) and, if needed, "
                "point Quest Tools at it: questtools config set scrcpy_path C:\\path\\to\\scrcpy.exe")


def register(sub, common):
    p = dry(add(sub, common, "install-scrcpy", "Download/update scrcpy for the live view (into Quest Tools' folder)",
                cmd_install))
    p.add_argument("--update", action="store_true", help="re-download even if this version is already installed")
    p = dry(add(sub, common, "mirror", "Live view of the headset on the PC (needs scrcpy)", cmd_mirror,
                aliases=["view"], description="Shows the headset picture in a window on the PC using scrcpy. "
                "By default only one eye is shown; the proximity sensor is overridden while the window is "
                "open so the display keeps running without wearing the headset."))
    p.add_argument("--eye", choices=["left", "right", "both"], default="left", help="which part of the panel")
    p.add_argument("--size", type=int, default=1440, help="max. window resolution (longest side)")
    p.add_argument("--bitrate", default="16M", help="video bitrate, e.g. 8M, 16M, 24M")
    p.add_argument("--fps", type=int, default=60, help="max. frame rate")
    p.add_argument("--audio", action="store_true", help="also forward audio (Android 11+)")
    p.add_argument("--record", metavar="FILE", help="additionally record to an .mp4/.mkv file")
    p.add_argument("--no-wake", action="store_true", help="do not wake the headset / override the sensor")
    p.add_argument("--install-scrcpy", action="store_true",
                   help="download/update scrcpy into Quest Tools' folder (also happens automatically when missing)")


def cmd_install(ctx):
    from ..scrcpy_install import install, latest_release
    if ctx.dry:
        info = latest_release()
        ui.info(f"Dry run: would download {info['name']} ({ui.human_bytes(info['size'])}), "
                f"SHA-256 {info['sha256'][:16]}…")
        return
    exe = install(force=ctx.args.update)
    ctx.emit({"scrcpy": exe})


def find_scrcpy() -> str | None:
    cand = [store.config().get("scrcpy_path"), os.environ.get("QUESTTOOLS_SCRCPY"), shutil.which("scrcpy")]
    home = Path.home()
    local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
    cand += [str(local / "Microsoft" / "WinGet" / "Links" / "scrcpy.exe"),
             str(home / "scoop" / "shims" / "scrcpy.exe"), r"C:\scrcpy\scrcpy.exe"]
    cand += [str(p) for p in sorted(home.glob("scrcpy*/scrcpy.exe"), reverse=True)]
    cand += [str(p) for p in sorted((local / "Microsoft" / "WinGet" / "Packages").glob("Genymobile.scrcpy*/*/scrcpy.exe"),
                                    reverse=True)]
    for x in cand:
        if x and Path(x).is_file():
            return x
    return None


def build_args(exe: str, serial: str, panel: tuple[int, int] | None, a, title: str) -> list[str]:
    bitrate = a.bitrate.upper()
    if not re.fullmatch(r"\d+[KM]?", bitrate):
        raise AdbError("Bitrate like 8M or 16M.")
    args = [exe, "-s", serial, "--window-title", title, f"--max-size={a.size}", f"--video-bit-rate={bitrate}",
            f"--max-fps={a.fps}"]
    if panel and a.eye != "both":
        w, h = panel
        eye_w = w // 2
        x = 0 if a.eye == "left" else eye_w
        args.append(f"--crop={eye_w}:{h}:{x}:0")
    if not a.audio:
        args.append("--no-audio")
    if a.record:
        args.append(f"--record={a.record}")
    return args


def cmd_mirror(ctx):
    from ..scrcpy_install import install, latest_release
    exe = find_scrcpy()
    if ctx.args.install_scrcpy and not ctx.dry:
        exe = install(force=True)
    elif not exe and not ctx.dry:
        ui.warn("The live view needs scrcpy (free, open source: github.com/Genymobile/scrcpy).")
        try:
            info = latest_release()
            question = (f"Download scrcpy {info['version']} ({ui.human_bytes(info['size'])}) from GitHub and install "
                        "it for Quest Tools?")
        except AdbError:
            question = "Download scrcpy from GitHub and install it for Quest Tools?"
        if not ui.confirm(question, ctx.yes, default=True):
            raise AdbError("scrcpy was not found.", INSTALL_HINT)
        exe = install()
    dev = ctx.dev
    m = re.search(r"Physical size:\s*(\d+)x(\d+)", dev.sh("wm size").out)
    panel = (int(m.group(1)), int(m.group(2))) if m else None
    if ctx.args.record and not Path(ctx.args.record).parent.exists():
        raise AdbError(f"Folder for {ctx.args.record} does not exist.")
    args = build_args(exe or "scrcpy", dev.serial, panel, ctx.args, f"Quest Tools - {dev.model} ({ctx.args.eye})")
    if ctx.dry:
        ui.info("Dry run: " + " ".join(args))
        if not exe:
            ui.warn("scrcpy is not installed.")
            ui.hint(INSTALL_HINT.splitlines()[0])
        return
    wake = not ctx.args.no_wake and dev.is_quest
    if wake:
        dev.sh("am broadcast -a com.oculus.vrpowermanager.prox_close; input keyevent KEYCODE_WAKEUP")
    ui.ok(f"Mirroring {dev.model} ({ctx.args.eye} eye) - close the window to stop.")
    if wake:
        ui.hint("Proximity sensor is overridden while mirroring; it is restored afterwards.")
    env = dict(os.environ, ADB=find_adb())  # scrcpy uses the same adb as Quest Tools
    try:
        rc = subprocess.call(args, env=env)
    except KeyboardInterrupt:
        rc = 0
    finally:
        if wake:
            dev.sh("am broadcast -a com.oculus.vrpowermanager.automation_disable")
    if rc not in (0, 2):  # 2 = window closed while connecting
        raise AdbError(f"scrcpy ended with code {rc}.", "Try a lower --size or --bitrate, or update scrcpy.")
    if ctx.args.record:
        ui.ok(f"Recording saved: {ctx.args.record}")
