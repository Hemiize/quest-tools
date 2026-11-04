"""File transfer with visible progress (name, MB done/total, percent)."""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

from . import ui
from .adb import AdbError, Device, popen, quote


def remote_size(dev: Device, path: str) -> int | None:
    r = dev.sh(f"du -sk {quote(path)} 2>/dev/null | cut -f1; stat -c %s {quote(path)} 2>/dev/null")
    nums = [int(x) for x in r.out.split() if x.isdigit()]
    if len(nums) >= 2 and nums[1] > 0:
        # file: stat is exact; folder: du (KiB)
        return nums[1] if nums[1] >= nums[0] * 1024 - 8192 else nums[0] * 1024
    return nums[0] * 1024 if nums else None


def _local_size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    if p.is_dir():
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    return 0


def pull(dev: Device, remote: str, local: Path, name: str | None = None) -> Path:
    total = remote_size(dev, remote)
    local = Path(local)
    target = local / Path(remote).name if local.is_dir() else local
    target.parent.mkdir(parents=True, exist_ok=True)
    prog = ui.Progress(name or Path(remote).name, total)
    proc = popen(["-s", dev.serial, "pull", remote, str(local)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    while proc.poll() is None:
        prog.update(_local_size(target))
        time.sleep(0.2)
    out, err = proc.communicate()
    if proc.returncode != 0:
        if not ui.JSON_MODE:
            print()
        raise AdbError(f"adb pull {remote} failed.", (err or out).decode("utf-8", "replace"))
    done = _local_size(target)
    prog.finish(done)
    if total and abs(done - total) > max(8192, total * 0.02) and target.is_file():
        raise AdbError(f"Size mismatch: {done} instead of {total} bytes.")
    return target


def push(dev: Device, local: Path, remote: str) -> None:
    local = Path(local)
    if not local.exists():
        raise AdbError(f"{local} does not exist.")
    total = _local_size(local)
    remote_target = remote.rstrip("/") + "/" + local.name if remote.endswith("/") else remote
    prog = ui.Progress(local.name, total)
    proc = popen(["-s", dev.serial, "push", str(local), remote], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    last = 0
    while proc.poll() is None:
        sz = remote_size(dev, remote_target) if local.is_file() else None
        if sz is not None:
            last = sz
        prog.update(last)
        time.sleep(0.4)
    out, err = proc.communicate()
    if proc.returncode != 0:
        if not ui.JSON_MODE:
            print()
        raise AdbError(f"adb push {local.name} failed.", (err or out).decode("utf-8", "replace"))
    prog.finish(total)
    m = re.search(r"(\d+) files? pushed", out.decode("utf-8", "replace"))
    if m and not ui.JSON_MODE and local.is_dir():
        ui.hint(f"{m.group(1)} files transferred")


def safe_name(s: str) -> str:
    return re.sub(r"[^\w.\-]", "_", s)


def default_dir(kind: str) -> Path:
    base = Path(os.environ.get("QUESTTOOLS_OUT", Path.home() / "Quest"))
    p = base / kind
    p.mkdir(parents=True, exist_ok=True)
    return p
