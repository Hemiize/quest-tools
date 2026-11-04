"""wifi, offline, dns: ADB over Wi-Fi and network control via Private DNS."""

from __future__ import annotations

import ipaddress
import re
import time

from .. import ops, store, ui
from ..adb import AdbError, list_devices, run_raw
from ..ui import c
from . import add, dry, group

BLACKHOLE = "blackhole.invalid"
DNS_PRESETS = {
    "adguard": ("dns.adguard-dns.com", "AdGuard: blocks ads and trackers"),
    "adguard-family": ("family.adguard-dns.com", "AdGuard Family: adds parental filtering"),
    "mullvad": ("base.dns.mullvad.net", "Mullvad: ads, trackers, malware"),
    "quad9": ("dns.quad9.net", "Quad9: blocks malware domains"),
    "cloudflare": ("one.one.one.one", "Cloudflare: fast, no filtering"),
}


def register(sub, common):
    _, g = group(sub, common, "wifi", "Connect/disconnect ADB over Wi-Fi", aliases=["w"])
    p = add(g, common, "connect", "Connect via Wi-Fi (without an IP: set up automatically over USB)", cmd_connect)
    p.add_argument("host", nargs="?", help="IP or IP:port")
    p.add_argument("-p", "--port", type=int, default=5555)
    p = add(g, common, "disconnect", "Disconnect the Wi-Fi connection", cmd_disconnect)
    p.add_argument("host", nargs="?")
    p = add(g, common, "pair", "Android 11 pairing (IP:port and 6-digit code)", cmd_pair)
    p.add_argument("endpoint")
    p.add_argument("code")
    add(g, common, "ip", "Show the headset's IP address", cmd_ip)

    p = dry(add(sub, common, "offline", "Cut the headset off from the internet (DNS blackhole, LAN keeps working)", cmd_offline))
    p.add_argument("state", nargs="?", choices=["on", "off", "status"], default="status")
    p = dry(add(sub, common, "dns", "Private DNS: " + ", ".join(DNS_PRESETS) + ", auto, off, <hostname>",
                cmd_dns))
    p.add_argument("target", nargs="?", help="Preset, hostname, auto or off")


def _ip_of(dev) -> str | None:
    m = re.search(r"\bsrc (\d+\.\d+\.\d+\.\d+)", dev.sh("ip route get 1").out)
    return m.group(1) if m else None


def cmd_connect(ctx):
    host = ctx.args.host
    port = ctx.args.port
    if not host:
        # Automatic: USB device → tcpip → read IP → connect
        usb = [d for d in list_devices() if d.state == "device" and not d.wifi]
        if not usb:
            last = store.config().get("last_wifi")
            if not last:
                raise AdbError("No USB device and no known Wi-Fi address.", "questtools wifi connect <ip>")
            host, port = last.rsplit(":", 1)
            ui.info(f"Trying last known address {last} …")
        else:
            from ..adb import Device
            dev = Device(ctx.args.serial or usb[0].serial, usb[0])
            ip = _ip_of(dev)
            if not ip:
                raise AdbError("The headset has no Wi-Fi connection.")
            with ui.Spinner(f"Enabling ADB over Wi-Fi (port {port}) …"):
                r = dev.run("tcpip", str(port))
                if not r.ok:
                    raise AdbError("adb tcpip failed.", r.out + r.err)
                time.sleep(2)
            host = ip
    else:
        if ":" in host:
            host, p = host.rsplit(":", 1)
            if not p.isdigit() or not 0 < int(p) < 65536:
                raise AdbError(f"Invalid port: {p}")
            port = int(p)
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if not re.fullmatch(r"[\w.\-]+", host):
                raise AdbError(f"Invalid address: {host}")
    ep = f"{host}:{port}"
    with ui.Spinner(f"Connecting to {ep} …"):
        for _ in range(3):
            r = run_raw(["connect", ep], timeout=15)
            if "connected to" in r.out and "cannot" not in r.out:
                break
            time.sleep(1.5)
    if "connected to" not in r.out or "cannot" in r.out:
        raise AdbError(f"Connection to {ep} failed.", (r.out + r.err).strip()
                       + "\nAfter a headset reboot, ADB over Wi-Fi has to be re-enabled over USB.")
    store.set_config("last_wifi", ep)
    ui.ok(f"Connected to {ep}")
    ui.hint(f"From now on without a cable: questtools -s {ep} status   (or just questtools status if only Wi-Fi is attached)")


def cmd_disconnect(ctx):
    args = ["disconnect"] + ([ctx.args.host] if ctx.args.host else [])
    r = run_raw(args, timeout=10)
    ui.ok(r.text or "Disconnected.")


def cmd_pair(ctx):
    if not re.fullmatch(r"\d{6}", ctx.args.code):
        raise AdbError("The pairing code has 6 digits.")
    r = run_raw(["pair", ctx.args.endpoint, ctx.args.code], timeout=30)
    if "Successfully paired" not in r.out:
        raise AdbError("Pairing failed.", r.out + r.err)
    ui.ok(r.text)


def cmd_ip(ctx):
    ip = _ip_of(ctx.dev)
    if ctx.emit({"ip": ip}):
        return
    ui.info(f"IP: {ip}" if ip else "No Wi-Fi connection.")


def _dns_state(dev) -> tuple[str | None, str | None]:
    return dev.setting("global", "private_dns_mode"), dev.setting("global", "private_dns_specifier")


def _set_dns(ctx, label: str, mode: str | None, host: str | None):
    batch = None if ctx.dry else store.Batch(ctx.dev.hwid, label)
    r1 = ops.put_setting(ctx.dev, batch, "global", "private_dns_mode", mode, dry=ctx.dry)
    r2 = ops.put_setting(ctx.dev, batch, "global", "private_dns_specifier", host, dry=ctx.dry) \
        if host is not None or mode != "hostname" else "unchanged"
    if ctx.dry:
        ui.info(f"Dry run: private_dns_mode={mode}, private_dns_specifier={host}")
    elif "changed" in (r1, r2):
        ui.hint(f"Undo: questtools undo {batch.id}")
    return "changed" in (r1, r2)


def cmd_offline(ctx):
    z = ctx.args.state
    mode, host = _dns_state(ctx.dev)
    offline = mode == "hostname" and (host or "").endswith(".invalid")
    if z == "status":
        if ctx.emit({"offline": offline, "mode": mode, "host": host}):
            return
        if offline:
            ui.warn("Offline mode is ON - internet blocked, LAN (e.g. Virtual Desktop via IP) still works.")
        else:
            ui.info(f"Online. Private DNS: {mode or 'default'}{' → ' + host if host else ''}")
        return
    if z == "on":
        if offline:
            return ui.info("Offline mode is already on.")
        if not ctx.dry:
            store.set_config("dns_before_offline", {"mode": mode, "host": host})
        _set_dns(ctx, "offline on", "hostname", BLACKHOLE)
        if ctx.dry:
            return
        ui.ok("Offline: all DNS lookups go nowhere. Meta servers, Store and updates are blocked.")
        ui.hint("LAN via IP keeps working. Back online: questtools offline off")
    else:
        if not offline:
            return ui.info("Offline mode is already off.")
        prev = store.config().get("dns_before_offline") or {}
        if isinstance(prev, str):  # format of older versions
            prev = {"mode": "hostname", "host": prev}
        p_mode, p_host = prev.get("mode"), prev.get("host")
        _set_dns(ctx, "offline off", p_mode, p_host if p_mode == "hostname" else None)
        if ctx.dry:
            return
        if not ctx.dry:
            store.set_config("dns_before_offline", None)
        ui.ok("Back online (Private DNS: " + (p_host if p_mode == "hostname" else p_mode or "default") + ").")


def cmd_dns(ctx):
    t = ctx.args.target
    if not t:
        mode, host = _dns_state(ctx.dev)
        if ctx.emit({"mode": mode, "host": host}):
            return
        ui.banner("Private DNS", str(ctx.dev))
        ui.kv([("Mode", mode or "default (automatic)"), ("Server", host)])
        ui.section("Presets")
        ui.table(["Name", "Server", "Effect"], [[n, h, c(d, "grey")] for n, (h, d) in DNS_PRESETS.items()])
        return
    if t == "off":
        _set_dns(ctx, "dns off", "off", None)
        if ctx.dry:
            return
        return ui.ok("Private DNS off (router DNS is used).")
    if t == "auto":
        _set_dns(ctx, "dns auto", None, None)
        if ctx.dry:
            return
        return ui.ok("Private DNS automatic (default).")
    host = DNS_PRESETS.get(t, (t, ""))[0]
    if not re.fullmatch(r"[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)+", host):
        raise AdbError(f"Not a valid hostname: {host}")
    _set_dns(ctx, f"dns {t}", "hostname", host)
    if ctx.dry:
        return
    ui.ok(f"Private DNS: {host}")
    ui.hint("Check: open a page in the headset. If nothing loads → questtools dns auto")
