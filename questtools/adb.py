"""ADB layer: find the binary, pick a device, run commands in a structured way.

adb is always started with an argument list (no local shell parsing).
Several device commands can be bundled into ONE `adb shell` call
(`Device.batch`), which makes status queries much faster.
"""

from __future__ import annotations

import ipaddress
import os
import re
import shutil
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
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
        found = reconnect_wifi()
        if found:
            return Device(found)
        raise AdbError("No device connected.",
                       "Plug in the USB cable and allow debugging, or: questtools wifi connect <ip>")
    if len(ready) == 1:
        d = ready[0]
        if not d.wifi:
            keep_wifi_alive(d.serial)
        return Device(d.serial, d)
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


# --------------------------------------------------------------------------- Wi-Fi ADB

def _connect(ep: str, timeout: float = 6) -> bool:
    try:
        r = run_raw(["connect", ep], timeout=timeout)
    except AdbError:
        return False
    return "connected to" in r.out and "cannot" not in r.out and "failed" not in r.out


def local_subnets() -> list[str]:
    """Private /24 networks of this PC, e.g. ['192.168.2']."""
    primary = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packet is sent; picks the default interface
            primary.append(s.getsockname()[0])
    except OSError:
        pass
    others = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            others.add(info[4][0])
    except OSError:
        pass
    nets = []
    # the default-route network first (the home LAN), virtual adapters (WSL, Hyper-V) after it
    for ip in primary + sorted(others - set(primary), key=lambda x: (not x.startswith("192.168."), x)):
        a = ipaddress.ip_address(ip)
        if a.is_private and not a.is_loopback and not a.is_link_local:
            net = ip.rsplit(".", 1)[0]
            if net not in nets:
                nets.append(net)
    return nets[:4]


def scan_adb_port(port: int = 5555, timeout: float = 0.3, nets: list[str] | None = None) -> list[str]:
    """IPs in the local /24 networks that accept TCP connections on the adb port."""
    hosts = [f"{n}.{i}" for n in (nets or local_subnets()) for i in range(1, 255)]

    def probe(ip):
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                return ip
        except OSError:
            return None
    with ThreadPoolExecutor(max_workers=128) as ex:
        return [ip for ip in ex.map(probe, hosts) if ip]


def find_wifi_headsets(port: int = 5555, known_only: bool = True) -> list[tuple[str, str]]:
    """Scans the LAN and connects to headsets with Wi-Fi ADB enabled. -> [(ip:port, hwid)]"""
    known = store.config().get("known", {})
    out = []
    for ip in scan_adb_port(port):
        ep = f"{ip}:{port}"
        if not _connect(ep):
            continue
        hw = run_raw(["-s", ep, "shell", "getprop ro.serialno"], timeout=8).text
        model = run_raw(["-s", ep, "shell", "getprop ro.product.model"], timeout=8).text
        if (known_only and hw in known) or (not known_only and ("quest" in model.lower() or hw in known)):
            out.append((ep, hw))
        else:
            run_raw(["disconnect", ep], timeout=5)
    return out


def reconnect_wifi() -> str | None:
    """No device attached: try the last address, the last IPs of known headsets, then a LAN scan."""
    cfg = store.config()
    tried = []
    for ep in [cfg.get("last_wifi")] + [f"{k['ip']}:{k.get('port') or 5555}" for k in cfg.get("known", {}).values()
                                        if k.get("ip")]:
        if ep and ep not in tried:
            tried.append(ep)
            if _connect(ep, timeout=4):
                return ep
    if cfg.get("known") and cfg.get("auto_find", True):
        found = find_wifi_headsets()
        if found:
            store.set_config("last_wifi", found[0][0])
            return found[0][0]
    return None


_kept_alive: set[str] = set()


def keep_wifi_alive(usb_serial: str, port: int = 5555) -> str | None:
    """With "wifi_keep" on: re-enable Wi-Fi ADB whenever the headset is on USB but tcpip mode is off
    (after every reboot). Returns the Wi-Fi endpoint if it was (re)enabled."""
    if not store.config().get("wifi_keep") or usb_serial in _kept_alive or os.environ.get("QUESTTOOLS_NO_KEEPALIVE"):
        return None
    _kept_alive.add(usb_serial)
    if run_raw(["-s", usb_serial, "shell", "getprop service.adb.tcp.port"], timeout=8).text:
        return None  # already listening
    m = re.search(r"\bsrc (\d+\.\d+\.\d+\.\d+)", run_raw(["-s", usb_serial, "shell", "ip route get 1"], timeout=8).out)
    if not m:
        return None  # headset not on Wi-Fi
    run_raw(["-s", usb_serial, "tcpip", str(port)], timeout=15)
    # adbd restarts - wait until the USB connection is back before continuing
    for _ in range(30):
        time.sleep(0.5)
        if any(d.serial == usb_serial and d.state == "device" for d in list_devices()):
            break
    ep = f"{m.group(1)}:{port}"
    if _connect(ep):
        store.set_config("last_wifi", ep)
        import sys
        print(f"  (keep-alive: Wi-Fi ADB re-enabled at {ep})", file=sys.stderr)
        return ep
    return None


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
