"""ADB layer: find the binary, pick a device, run commands in a structured way.

adb is always started with an argument list (no local shell parsing).
Several device commands can be bundled into ONE `adb shell` call
(`Device.batch`), which makes status queries much faster.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import store

PKG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")
PROP_RE = re.compile(r"^[A-Za-z0-9_.\-:]+$")
WIFI_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}:\d+$")

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class AdbError(Exception):
    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.message = message
        self.detail = detail.strip()


@dataclass
class Result:
    code: int | None
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def text(self) -> str:
        return self.out.strip()

    def lines(self) -> list[str]:
        return [ln.rstrip("\r") for ln in self.out.splitlines()]


def find_adb() -> str:
    cfg = store.config().get("adb_path")
    for cand in (cfg, os.environ.get("QUESTTOOLS_ADB")):
        if cand and Path(cand).is_file():
            return cand
    found = shutil.which("adb")
    if found:
        return found
    for cand in (r"C:\platform-tools\adb.exe",
                 os.path.expandvars(r"%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe"),
                 os.path.expanduser("~/Android/Sdk/platform-tools/adb")):
        if Path(cand).is_file():
            return cand
    raise AdbError("adb not found.",
                   "Install Platform-Tools or set the path: questtools config set adb_path <path>")


def find_fastboot() -> str | None:
    found = shutil.which("fastboot")
    if found:
        return found
    try:
        cand = Path(find_adb()).with_name("fastboot.exe" if os.name == "nt" else "fastboot")
        return str(cand) if cand.is_file() else None
    except AdbError:
        return None


def run_raw(args: list[str], timeout: float | None = 30, binary: bytes | None = None,
            capture_bytes: bool = False) -> Result:
    exe = find_adb()
    try:
        p = subprocess.run([exe, *args], input=binary, capture_output=True, timeout=timeout,
                           creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise AdbError(f"Timed out after {timeout:.0f}s: adb {' '.join(args[:4])}")
    except OSError as e:
        raise AdbError("Could not start adb.", str(e))
    if capture_bytes:
        return Result(p.returncode, p.stdout, p.stderr.decode("utf-8", "replace"))  # type: ignore[arg-type]
    # adb shell returns CRLF on Windows - normalize it, otherwise regexes on r.out fail
    return Result(p.returncode, p.stdout.decode("utf-8", "replace").replace("\r\n", "\n"),
                  p.stderr.decode("utf-8", "replace").replace("\r\n", "\n"))


def popen(args: list[str], **kw) -> subprocess.Popen:
    return subprocess.Popen([find_adb(), *args], creationflags=_NO_WINDOW, **kw)


# --------------------------------------------------------------------------- Devices


@dataclass
class DeviceEntry:
    serial: str
    state: str
    model: str = ""
    product: str = ""
    transport_id: str = ""

    @property
    def wifi(self) -> bool:
        return bool(WIFI_RE.match(self.serial)) or self.serial.startswith("adb-")


def list_devices() -> list[DeviceEntry]:
    r = run_raw(["devices", "-l"], timeout=15)
    devs = []
    for line in r.lines():
        line = line.strip()
        if not line or line.startswith("List of") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        e = DeviceEntry(parts[0], parts[1])
        for tok in parts[2:]:
            if ":" in tok:
                k, v = tok.split(":", 1)
                if k == "model":
                    e.model = v.replace("_", " ")
                elif k == "product":
                    e.product = v
                elif k == "transport_id":
                    e.transport_id = v
        devs.append(e)
    return devs


class Device:
    def __init__(self, serial: str, entry: DeviceEntry | None = None):
        self.serial = serial
        self.entry = entry
        self._props: dict[str, str] | None = None
        self._hwid: str | None = None

    # -- Execution -----------------------------------------------------------
    def run(self, *args: str, timeout: float | None = 30, **kw) -> Result:
        return run_raw(["-s", self.serial, *args], timeout=timeout, **kw)

    def sh(self, cmd: str, timeout: float | None = 30) -> Result:
        return self.run("shell", cmd, timeout=timeout)

    def sh_ok(self, cmd: str, what: str, timeout: float | None = 30) -> Result:
        r = self.sh(cmd, timeout=timeout)
        if not r.ok or _looks_failed(r):
            raise AdbError(what, (r.err or r.out))
        return r

    def batch(self, cmds: dict[str, str], timeout: float | None = 40) -> dict[str, str]:
        """Runs several shell commands in one call; returns {name: stdout}."""
        parts = []
        for key, cmd in cmds.items():
            parts.append(f"echo '@@QT@@{key}'; {cmd} 2>/dev/null")
        r = self.sh("; ".join(parts), timeout=timeout)
        res: dict[str, str] = {k: "" for k in cmds}
        cur = None
        buf: list[str] = []
        for line in r.lines():
            if line.startswith("@@QT@@"):
                if cur is not None:
                    res[cur] = "\n".join(buf).strip()
                cur, buf = line[6:], []
            else:
                buf.append(line)
        if cur is not None:
            res[cur] = "\n".join(buf).strip()
        return res

    # -- Properties ----------------------------------------------------------
    def props(self, refresh: bool = False) -> dict[str, str]:
        if self._props is None or refresh:
            r = self.sh("getprop")
            self._props = parse_getprop(r.out)
        return self._props

    def prop(self, key: str) -> str:
        return self.props().get(key, "")

    @property
    def hwid(self) -> str:
        """Hardware serial (ro.serialno). The journal, profiles and backups are keyed by it, so
        USB and Wi-Fi connections to the same headset share history."""
        if self._hwid is None:
            v = self._props.get("ro.serialno") if self._props else None
            self._hwid = v or self.sh("getprop ro.serialno").text or self.serial
        return self._hwid

    @property
    def model(self) -> str:
        return self.prop("ro.product.model") or (self.entry.model if self.entry else "") or "?"

    @property
    def is_quest(self) -> bool:
        p = self.props()
        return ("quest" in p.get("ro.product.model", "").lower()
                or p.get("ro.product.manufacturer", "").lower() in ("oculus", "meta"))

    def setting(self, ns: str, key: str) -> str | None:
        v = self.sh(f"settings get {ns} {key}").text
        return None if v == "null" else v

    def packages(self, flag: str = "") -> set[str]:
        r = self.sh(f"pm list packages {flag}".strip(), timeout=60)
        return {ln.split(":", 1)[1].strip() for ln in r.lines() if ln.startswith("package:")}

    def __str__(self) -> str:
        return f"{self.model} ({self.serial})"


def parse_getprop(text: str) -> dict[str, str]:
    props = {}
    for line in text.splitlines():
        m = re.match(r"^\[(.+?)\]: \[(.*)\]\s*$", line.strip())
        if m:
            props[m.group(1)] = m.group(2)
    return props


def _looks_failed(r: Result) -> bool:
    t = (r.out + r.err).lower()
    return any(s in t for s in ("exception", "error:", "failure", "not allowed", "permission denial",
                                "unknown package", "cannot disable", "securityexception"))


def check_pkg(pkg: str) -> str:
    if not PKG_RE.match(pkg):
        raise AdbError(f"Invalid package name: {pkg!r}")
    return pkg


def check_prop(key: str) -> str:
    if not PROP_RE.match(key):
        raise AdbError(f"Invalid property/setting name: {key!r}")
    return key


def quote(s: str) -> str:
    """Quoting for the device shell (POSIX sh)."""
    return "'" + s.replace("'", "'\\''") + "'"


def pick(serial: str | None = None) -> Device:
    """Picks the target device: --serial > QUESTTOOLS_SERIAL > only/preferred device.

    If the same headset is attached via both USB and Wi-Fi, USB is preferred.
    """
    serial = serial or os.environ.get("QUESTTOOLS_SERIAL")
    devs = list_devices()
    if serial:
        for d in devs:
            if d.serial == serial:
                _check_state(d)
                return Device(d.serial, d)
        if WIFI_RE.match(serial):
            r = run_raw(["connect", serial], timeout=10)
            if "connected" in r.out:
                return Device(serial)
        raise AdbError(f"Device {serial} is not connected.", _devlist(devs))
    ready = [d for d in devs if d.state == "device"]
    if not ready:
        if devs:
            _check_state(devs[0])
        last = store.config().get("last_wifi")
        if last:
            r = run_raw(["connect", last], timeout=8)
            if "connected" in r.out and "cannot" not in r.out:
                return Device(last)
        raise AdbError("No device connected.",
                       "Plug in the USB cable and allow debugging, or: questtools wifi connect <ip>")
    if len(ready) == 1:
        return Device(ready[0].serial, ready[0])
    pref = store.config().get("default_serial")
    for d in ready:
        if d.serial == pref:
            return Device(d.serial, d)
    usb = [d for d in ready if not d.wifi]
    if len(usb) == 1:
        # Same headset over USB and Wi-Fi? Only then is it safe to just take USB.
        hw = {run_raw(["-s", d.serial, "shell", "getprop ro.serialno"], timeout=10).text for d in ready}
        if len(hw) == 1:
            return Device(usb[0].serial, usb[0])
    raise AdbError("Multiple devices connected - pick one with -s <serial>.", _devlist(ready))


def _check_state(d: DeviceEntry) -> None:
    if d.state == "unauthorized":
        raise AdbError(f"{d.serial}: debugging not authorized.",
                       "Confirm the \"Allow USB debugging\" prompt in the headset.")
    if d.state == "offline":
        raise AdbError(f"{d.serial} is offline.", "Replug the cable or run: adb kill-server")
    if d.state != "device":
        raise AdbError(f"{d.serial} is in state \"{d.state}\".")


def _devlist(devs: list[DeviceEntry]) -> str:
    return "\n".join(f"  {d.serial}  {d.state}  {d.model}" for d in devs) or "  (none)"
