"""Interactive interface: arrow-key menus, live status at the top, every feature reachable."""

from __future__ import annotations

import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from . import __version__, battery, kb, probe, store, term, transfer, ui, updates
from .adb import AdbError, list_devices, pick
from .ui import c


@dataclass
class Item:
    label: str
    value: Any = None
    hint: str = ""
    desc: str = ""
    key: str | None = None
    style: str | None = None
    checked: bool = False
    search: str = ""


@dataclass
class Status:
    data: dict | None = None
    error: str | None = None
    loading: bool = False
    version: int = 0
    conn: dict = field(default_factory=dict)
    update: dict | None = None


GRADIENT = [141, 141, 177, 177, 213, 213, 219, 219, 225, 225, 231]
SPIN = "|/-\\"


def _logo() -> str:
    text = "QUEST TOOLS"
    out = []
    for i, ch in enumerate(" ".join(text)):
        col = GRADIENT[min(i // 2, len(GRADIENT) - 1)]
        out.append(f"\x1b[1;38;5;{col}m{ch}\x1b[0m" if ui.COLOR else ch)
    return "".join(out)


class App:
    def __init__(self, serial: str | None = None):
        self.serial = serial
        self.st = Status()
        self._lock = threading.Lock()
        self.frame = 0

    # ------------------------------------------------------------------ Status
    def refresh(self, wait: bool = False):
        if self.st.loading:
            return
        self.st.loading = True

        def work():
            try:
                devs = list_devices()
                dev = pick(self.serial)
                data = probe.collect(dev)
                upd = updates.track(data)
                ready = [d for d in devs if d.state == "device"]
                conn = {"usb": any(not d.wifi for d in ready), "wifi": [d.serial for d in ready if d.wifi],
                        "count": len({d.model for d in ready})}
                with self._lock:
                    self.st.data, self.st.error, self.st.conn, self.st.update = data, None, conn, upd
                    if not self.serial and len(ready) > 1 and conn["count"] > 1:
                        self.st.error = "Multiple devices – pick one under Connection → Select device"
            except AdbError as e:
                with self._lock:
                    self.st.data, self.st.error = None, e.message
            except Exception as e:  # the status must never crash the interface
                with self._lock:
                    self.st.error = f"Status unreadable: {e}"
            finally:
                self.st.loading = False
                self.st.version += 1

        t = threading.Thread(target=work, daemon=True)
        t.start()
        if wait:
            t.join()

    def dev(self):
        return pick(self.serial)

    # ------------------------------------------------------------------ Header
    def header(self) -> list[str]:
        w = min(term.size()[0] - 2, 118)
        inner = w - 4
        top = c("┌─ ", "accent") + _logo() + " " + c("─" * max(0, inner - 26 - len(__version__)), "accent") \
            + c(f" v{__version__} ─┐", "accent")
        rows = self._header_rows(inner)
        lines = [top]
        for r in rows:
            lines.append(c("│ ", "accent") + ui.pad(ui.trunc(r, inner), inner) + c(" │", "accent"))
        lines.append(c("└" + "─" * (w - 2) + "┘", "accent"))
        return lines

    def _header_rows(self, inner: int) -> list[str]:
        s, err = self.st.data, self.st.error
        spin = c(SPIN[self.frame % len(SPIN)], "accent") + " " if self.st.loading else ""
        if not s:
            if err:
                return [c("● ", "red") + c("No device", "bold") + "  " + c(err, "grey"),
                        c("Plug in the USB cable and allow debugging – or open menu ", "grey") + c("9 Connection", "cyan"), ""]
            return [spin + c("Looking for headset …", "grey"), "", ""]
        os_txt = f"Horizon OS v{s['horizon_os']}" if s.get("horizon_os") else f"Android {s.get('android')}"
        conn = []
        if self.st.conn.get("usb"):
            conn.append("USB")
        if self.st.conn.get("wifi") or s.get("adb_tcp_port"):
            conn.append("Wi-Fi")
        serial = s["serial"]
        short = serial if len(serial) < 12 else serial[:4] + "…" + serial[-3:]
        r1 = (spin + c("● ", "green") + c(s.get("model") or "?", "bold", "white") + c("  ·  ", "grey") + os_txt
              + c("  ·  ", "grey") + c(short, "grey") + c("  ·  ", "grey") + c(" + ".join(conn) or "?", "cyan"))
        b = s["battery"]
        charging = c(" ↑", "yellow") if b.get("status") == "charging" or (b.get("ac") and b.get("status") != "full") else ""
        parts = [c("Battery ", "grey") + self._lvl(b.get("level")) + charging]
        ctrls = s.get("controllers") or []
        if ctrls:
            parts.append(c("Controllers ", "grey") + "  ".join(
                f"{x['side'][0].upper()} {self._pct(x['battery'])}" for x in ctrls))
        t = s.get("temps", {})
        hot = max([v for v in (t.get("cpu"), t.get("gpu"), b.get("temp_c")) if v] or [0])
        if hot:
            parts.append(c("Temp ", "grey") + c(f"{hot:.0f}°C", "green" if hot < 38 else "yellow" if hot < 45 else "red"))
        if s.get("refresh_hz"):
            parts.append(c(f"{s['refresh_hz']} Hz", "grey"))
        warn = battery.warnings(s)
        if warn:
            parts.append("  ".join(c("! " + t, "red" if lv == "err" else "yellow", "bold") for lv, t in warn))
        r2 = c("   ·   ", "grey").join(parts)
        dis = s.get("disabled", [])
        prof = store.config().get("profile", {}).get(s.get("hwid", serial))
        prof_txt = c(prof, "accent") if prof else c("none", "grey")
        net = c("OFFLINE", "yellow", "bold") if s.get("offline") else \
            (s["private_dns"]["host"] if s["private_dns"]["mode"] == "hostname" else "default DNS")
        perf_on = [k for k in ("cpu", "gpu", "ffr", "tex_w") if s["perf"].get(k)]
        r3 = (c("Disabled ", "grey") + str(len(dis)) + c("   ·   Profile ", "grey") + prof_txt
              + c("   ·   Network ", "grey") + net
              + (c("   ·   Performance ", "grey") + c("tuned", "yellow") if perf_on else ""))
        tracking = s["running"].get("trackingservice")
        if s.get("is_quest") and tracking is False:
            r3 += c("   ·   TRACKING OFF", "red", "bold")
        pend = (self.st.update or {}).get("pending")
        if pend:
            r3 += c(f"   ·   UPDATE re-enabled {len(pend['packages'])} packages → r", "yellow", "bold")
        return [r1, r2, r3]

    @staticmethod
    def _lvl(v) -> str:
        if v is None:
            return c("?", "grey")
        filled = round(v / 10)
        col = "green" if v > 40 else "yellow" if v > 15 else "red"
        return c("█" * filled, col) + c("░" * (10 - filled), "grey") + f" {v}%"

    @staticmethod
    def _pct(v) -> str:
        if v is None:
            return c("?", "grey")
        return c(f"{v}%", "green" if v > 40 else "yellow" if v > 15 else "red")

    # ------------------------------------------------------------------ Selection list
    def select(self, title: str, items: list[Item], *, multi: bool = False, hotkeys: bool = False,
               subtitle: str = "", start: int = 0, footer: str = "", tab: Callable | None = None,
               tab_label: str = "") -> Any:
        """Arrow-key list. Returns the value (a list when multi) or None on Esc."""
        idx, off, flt = start, 0, ""
        seen_version = -1
        term.hide_cursor()
        try:
            while True:
                vis = [it for it in items if not flt or flt.lower() in (it.label + " " + it.search + " "
                                                                         + ui.strip(it.hint)).lower()]
                idx = max(0, min(idx, len(vis) - 1))
                w, h = term.size()
                head = self.header()
                lines = head + [""]
                lines.append("  " + c(title.upper(), "accent", "bold") + ("  " + c(subtitle, "grey") if subtitle else ""))
                if not hotkeys and (flt or len(items) > 10):
                    lines.append("  " + c("Filter: ", "grey") + (c(flt, "bold") + c("_", "accent") if flt
                                                                else c("type to search", "grey")))
                lines.append("")
                desc_h = 3
                room = max(4, h - len(lines) - desc_h - 4)
                if idx < off:
                    off = idx
                if idx >= off + room:
                    off = idx - room + 1
                lab_w = min(max([ui.width(it.label) for it in vis] + [10]) + 2, max(24, w // 2))
                for i, it in enumerate(vis[off:off + room], start=off):
                    sel = i == idx
                    mark = c("► ", "accent", "bold") if sel else "  "
                    box = ""
                    if multi:
                        box = c("■ ", "green") if it.checked else c("□ ", "grey")
                    key = c(f"{it.key} ", "accent") if hotkeys and it.key else ("  " if hotkeys else "")
                    label = it.label
                    if it.style:
                        label = c(label, it.style)
                    if sel:
                        label = c(ui.strip(label), "bold", "white")
                    lines.append("  " + mark + box + key + ui.pad(label, lab_w) + "  " + it.hint)
                if not vis:
                    lines.append("    " + c("(no matches)", "grey"))
                more = len(vis) - off - room
                lines.append(c(f"    ↓ {more} more", "grey") if more > 0 else "")
                cur = vis[idx] if vis else None
                lines.append("  " + c("─" * min(w - 4, 114), "grey"))
                d = cur.desc if cur else ""
                for dl in _wrap(d, w - 6)[:desc_h]:
                    lines.append("  " + c(dl, "grey"))
                lines += [""] * (desc_h - len(_wrap(d, w - 6)[:desc_h]))
                keys = ["↑↓ select", "Enter " + ("done" if multi else "open")]
                if multi:
                    keys.insert(1, "Space toggle")
                    keys.insert(2, "Ctrl+A all")
                if hotkeys:
                    keys.append("digit = jump")
                if tab:
                    keys.append(f"Tab {tab_label}")
                keys += ["Esc back", "F5 refresh"]
                lines.append("  " + c(" · ".join(keys), "grey") + ("   " + footer if footer else ""))
                term.paint(lines)
                seen_version = self.st.version

                k = self._key(seen_version)
                if k is None:
                    continue
                if k == "up":
                    idx = (idx - 1) % max(1, len(vis))
                elif k == "down":
                    idx = (idx + 1) % max(1, len(vis))
                elif k == "pgup":
                    idx = max(0, idx - room)
                elif k == "pgdn":
                    idx = min(len(vis) - 1, idx + room)
                elif k == "home":
                    idx = 0
                elif k == "end":
                    idx = len(vis) - 1
                elif k == "F5":
                    self.refresh()
                elif k == "tab" and tab:
                    items = tab() or items
                    idx = 0
                elif k == "space" and multi and cur:
                    cur.checked = not cur.checked
                    idx = min(idx + 1, len(vis) - 1)
                elif k == "ctrl-a" and multi:
                    on = not all(it.checked for it in vis)
                    for it in vis:
                        it.checked = on
                elif k == "enter":
                    if multi:
                        return [it.value for it in items if it.checked]
                    if cur:
                        return cur.value
                elif k in ("esc", "ctrl-c"):
                    if flt:
                        flt = ""
                        continue
                    return None
                elif k == "back":
                    if flt:
                        flt = flt[:-1]
                    else:
                        return None
                elif hotkeys and len(k) == 1:
                    hit = next((it for it in vis if it.key and it.key.lower() == k.lower()), None)
                    if hit:
                        return hit.value
                    if k == "q":
                        return "__quit__"
                elif not hotkeys and len(k) == 1 and k.isprintable():
                    flt += k
                    idx = 0
        finally:
            term.show_cursor()

    def _key(self, seen_version: int) -> str | None:
        """Waits for a key; redraws in between when the status update finishes."""
        if os.name == "nt":
            import msvcrt
            while True:
                if msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ch in ("\x00", "\xe0"):
                        ch2 = msvcrt.getwch()
                        if ch2 == "?":
                            return "F5"
                        return term._WIN_KEYS.get(ch2, "")
                    return term._map(ch)
                time.sleep(0.05)
                if self.st.loading:
                    self.frame += 1
                    return None
                if self.st.version != seen_version:
                    return None
        return term.read_key()

    # ------------------------------------------------------------------ Running commands
    def run(self, argv: list[str], title: str = "", pause: bool = True, refresh: bool = True) -> int:
        from .cli import main
        term.clear()
        for ln in self.header():
            print(ln)
        if title:
            print("\n  " + c(title.upper(), "accent", "bold"))
        term.show_cursor()
        base = ["-s", self.serial] if self.serial else []
        try:
            rc = main(base + argv)
        except KeyboardInterrupt:
            rc = 130
        except SystemExit as e:  # argparse
            rc = int(e.code or 0)
        if pause:
            term.wait_key()
        if refresh:
            self.refresh()
        return rc

    def message(self, text: str, level: str = "info"):
        term.clear()
        for ln in self.header():
            print(ln)
        print()
        {"info": ui.info, "ok": ui.ok, "warn": ui.warn, "err": ui.err}[level](text)
        term.wait_key()

    def busy(self, text: str):
        lines = self.header() + ["", "  " + c("► ", "accent") + text]
        term.paint(lines)

    def guard(self, fn: Callable, *a) -> Any:
        try:
            return fn(*a)
        except AdbError as e:
            self.message(e.message + (f"\n  {e.detail}" if e.detail else ""), "err")
            return None

    # ------------------------------------------------------------------ Main loop
    def loop(self):
        term.setup_window("Quest Tools")
        term.paint(self.header() + ["", "  " + c("Connecting to the headset …", "grey")])
        self.refresh(wait=True)
        self.offer_update_restore()
        menus = main_menu(self)
        idx = 0
        while True:
            items = [Item(m[1], i, hint=c(m[2], "grey"), desc=m[3], key=m[0]) for i, m in enumerate(menus)]
            choice = self.select("Main menu", items, hotkeys=True, start=idx,
                                 subtitle="arrow keys or digit", footer=c("q quit", "grey"))
            if choice in (None, "__quit__"):
                term.clear()
                print(c("  Quest Tools closed.", "grey"))
                return
            idx = choice
            try:
                menus[choice][4]()
            except KeyboardInterrupt:
                pass

    def offer_update_restore(self, always: bool = False):
        """After a firmware update: offer to switch the re-enabled packages off again."""
        pend = (self.st.update or {}).get("pending")
        hwid = (self.st.data or {}).get("hwid")
        if not pend or not hwid:
            if always:
                self.message("No firmware update re-enabled any of your packages.", "ok")
            return
        n = len(pend["packages"])
        listing = ", ".join(kb.short_name(p) for p in pend["packages"])
        ch = self.select(f"Firmware update v{pend['from']} → v{pend['to']}", [
            Item(f"Restore now - disable the {n} packages again", "go",
                 desc=f"A settings snapshot is taken first; every change can be undone. Packages: {listing}"),
            Item("Dry run - show what would happen", "dry", desc=listing),
            Item("Not now", "later", desc="Ask again next time (or use r Repair → After a firmware update)."),
            Item("Ignore this update", "ignore", desc="Keep the packages enabled and stop asking for this update."),
        ], subtitle=f"{n} packages you had disabled are active again")
        if ch == "go":
            self.run(["repair", "update"], "Restore after firmware update")
        elif ch == "dry":
            self.run(["repair", "update", "-n"], "Restore after firmware update (dry run)")
        elif ch == "ignore":
            updates.clear(hwid)
            self.st.update = None

    def submenu(self, title: str, entries: list[tuple]):
        """entries: (key, label, hint, description, action)"""
        idx = 0
        while True:
            items = [Item(e[1], i, hint=c(e[2], "grey"), desc=e[3], key=e[0]) for i, e in enumerate(entries)]
            ch = self.select(title, items, hotkeys=True, start=idx)
            if ch in (None, "__quit__"):
                return
            idx = ch
            try:
                entries[ch][4]()
            except KeyboardInterrupt:
                pass


def _wrap(text: str, width: int) -> list[str]:
    out = []
    for para in (text or "").split("\n"):
        line = ""
        for word in para.split():
            if len(line) + len(word) + 1 > width:
                out.append(line)
                line = word
            else:
                line = (line + " " + word).strip()
        out.append(line)
    return [x for x in out if x is not None]


# ====================================================================== Menus

def main_menu(app: App) -> list[tuple]:
    from .commands import debloat as dbm
    return [
        ("1", "Overview", "status, doctor, monitor", "Detailed status, health check with fixes, "
         "live monitor for CPU/RAM/temperatures, and device info.", lambda: overview(app)),
        ("2", "Privacy & Debloat", "telemetry, profiles, packages", "Turn off telemetry and Meta services, "
         "check and apply profiles, disable and enable individual packages, offline mode and Private DNS.",
         lambda: privacy(app)),
        ("3", "Performance", "CPU/GPU, foveation, resolution", "Clock levels, fixed foveated rendering, "
         "eye-buffer resolution, refresh rate, presets and process list.", lambda: performance(app)),
        ("4", "Headset", "HUD, Guardian, sensor, reboot", "FPS overlay, Guardian, proximity sensor, "
         "restart the VR shell, reboot, volume, keys and text input.", lambda: headset(app)),
        ("5", "Apps", "launch, back up, install", "All apps with version and storage size. Pick an app → "
         "launch, stop, back up APK, back up save game, permissions, uninstall. Install APKs.",
         lambda: apps_menu(app)),
        ("6", "Backups & Settings", "back up/restore settings", "Back up, compare and restore settings, "
         "package states and performance values. App backups with save games.",
         lambda: backups(app)),
        ("7", "Media", "screenshot, recording, logcat", "Screenshots (even while the headset sleeps), "
         "screen recording, filtered log.", lambda: media(app)),
        ("8", "Files", "file manager /sdcard", "Browse folders, download, upload and delete files, "
         "storage usage.", lambda: files_menu(app)),
        ("9", "Connection", "Wi-Fi ADB, select device", "Set up ADB over Wi-Fi (automatically via USB), "
         "connect/disconnect, pairing, select device.", lambda: connection(app)),
        ("0", "Tools", "shell, history, undo", "adb shell, command library, change log, "
         "undo changes, Quest Tools settings.", lambda: tools_menu(app)),
        ("m", "Modes", "streaming, battery, private", "Switch several settings at once - performance and "
         "network. One undo reverts a whole switch. Save your own modes.", lambda: modes_menu(app)),
        ("r", "Repair", "purchases, panels, tracking", "Detect and fix known problems: Store purchases gone "
         "or apps under 'Unknown sources', Store apps that will not install, missing home panels, dead "
         "controllers/tracking.", lambda: repair_menu(app)),
    ]


# ---------------------------------------------------------------------- 1 Overview

def overview(app: App):
    app.submenu("Overview", [
        ("1", "Status", "", "Battery, controllers, temperatures, system, performance, privacy.",
         lambda: app.run(["status"], "Status")),
        ("2", "Health check", "doctor", "Checks critical packages, tracking service, profile drift, "
         "firmware updates, battery and storage – with a fix command.", lambda: app.run(["doctor"], "Health check")),
        ("3", "Live monitor", "Ctrl+C stops", "CPU, RAM, temperatures and top processes, every 2 seconds.",
         lambda: app.run(["perf", "monitor"], "", pause=False)),
        ("4", "Device info", "", "System, build, bootloader, panel, storage, network.",
         lambda: app.run(["info"], "Device info")),
        ("5", "Connected devices", "", "All devices connected via USB/Wi-Fi.",
         lambda: app.run(["devices"], "Devices")),
        ("6", "Battery", "health, drain, history", "Headset and controller battery, temperature, estimated "
         "battery health, drain/charge rate and a history of samples.", lambda: app.run(["battery"], "Battery")),
    ])


# ---------------------------------------------------------------------- 2 Privacy

def _profile_items(with_desc=True) -> list[Item]:
    from .commands.debloat import _all_profiles
    return [Item(n, n, hint=c(f"{len(p.get('disable', []))} packages", "grey"), desc=p.get("description", ""))
            for n, p in _all_profiles().items()]


def privacy(app: App):
    def apply_profile():
        prof = app.select("Apply profile", _profile_items())
        if not prof:
            return
        mode = app.select(f"Profile '{prof}'", [
            Item("Dry run – just show", "dry", desc="Shows which packages would be disabled. Changes nothing."),
            Item("Apply", "go", desc="Disables the packages (asks first). Every change can be reverted "
                                     "with Undo.")])
        if mode:
            app.run(["debloat", "apply", prof] + (["-n"] if mode == "dry" else []), f"Profile {prof}")

    def check_profile():
        prof = app.select("Check profile", _profile_items())
        if prof:
            app.run(["debloat", "check", prof], f"Profile check {prof}")

    def pick_packages(disable: bool):
        from .commands import debloat as dbm
        dev = app.guard(app.dev)
        if not dev:
            return
        app.busy("Reading package lists …")
        rows = app.guard(dbm._scan, SimpleNamespace(dev=dev))
        if rows is None:
            return
        show_all = {"v": False}

        def build():
            out = []
            for r in sorted(rows, key=lambda r: (dbm.VERDICT_ORDER.index(r["verdict"]), r["package"])):
                if r["disabled"] == disable:
                    continue
                if not show_all["v"] and not (r["known"] or r["category"] in ("TELEMETRY", "META")):
                    continue
                lbl, sty = kb.VERDICT_LABEL[r["verdict"]]
                out.append(Item(r["package"], r["package"], hint=c(f"{lbl:<12}", sty) + c(r["note"][:60], "grey"),
                                desc=r["note"], style=None if r["known"] else "grey", search=lbl))
            return out

        def toggle_all():
            show_all["v"] = not show_all["v"]
            return build()

        items = build()
        if not items:
            return app.message("No matching packages.", "info")
        sel = app.select("Disable packages" if disable else "Enable packages", items, multi=True,
                         subtitle="Space to mark, Enter to run", tab=toggle_all,
                         tab_label="all/known")
        if sel:
            app.run(["debloat", "disable" if disable else "enable", *sel],
                    "Disable" if disable else "Enable")

    def explain():
        pkg = term.ask("Package name", hint="e.g. com.oculus.ocms")
        if pkg:
            app.run(["debloat", "explain", pkg], "Package explained")

    def offline():
        ch = app.select("Offline mode", [
            Item("Show status", "status"),
            Item("Go offline", "on", desc="Blocks all DNS lookups (blackhole). Meta, Store and updates "
                                          "are cut off; LAN by IP (Virtual Desktop) keeps working."),
            Item("Back online", "off", desc="Restores the previous Private DNS state.")])
        if ch:
            app.run(["offline", ch], "Offline mode")

    def dns():
        from .commands.net import DNS_PRESETS
        items = [Item(n, n, hint=c(h, "grey"), desc=d) for n, (h, d) in DNS_PRESETS.items()]
        items += [Item("Automatic (default)", "auto"), Item("Off (router DNS)", "off"),
                  Item("Custom hostname …", "__custom")]
        ch = app.select("Private DNS", items)
        if ch == "__custom":
            ch = term.ask("Hostname of the DNS-over-TLS server")
        if ch:
            app.run(["dns", ch], "Private DNS")

    def save_profile():
        name = term.ask("Name for the new profile", hint="Saves the currently disabled packages.")
        if name:
            app.run(["debloat", "save", name], "Save profile")

    app.submenu("Privacy & Debloat", [
        ("1", "Show telemetry", "", "All telemetry, advertising and data-collection components with status.",
         lambda: app.run(["debloat", "telemetry"], "Telemetry")),
        ("2", "Check profile", "after updates", "Shows which packages of a profile are active again – "
         "firmware updates often re-enable them.", check_profile),
        ("3", "Apply profile", "telemetry / privacy", "Pick a profile, review a dry run, apply.",
         apply_profile),
        ("4", "Disable packages", "pick list", "Known Meta/telemetry packages with a rating; Tab shows "
         "all packages. Packages marked 'keep' are skipped.", lambda: pick_packages(True)),
        ("5", "Enable packages", "pick list", "Turn disabled packages back on.",
         lambda: pick_packages(False)),
        ("6", "Analyze all packages", "", "Full list with verdict, status and note.",
         lambda: app.run(["debloat", "scan", "--all"], "Package analysis")),
        ("7", "Explain package", "", "What does a package do, and what happens when it is disabled?", explain),
        ("8", "Offline mode", "DNS blackhole", "Block internet access for the headset; LAN keeps working.", offline),
        ("9", "Private DNS", "AdGuard, Mullvad, …", "System-wide ad/tracker filtering via DNS-over-TLS.",
         dns),
        ("p", "Show profiles", "", "Built-in and custom profiles.", lambda: app.run(["debloat", "profiles"], "Profiles")),
        ("s", "Save current state as profile", "", "Custom profile from the currently disabled packages.",
         save_profile),
    ])


# ---------------------------------------------------------------------- 3 Performance

def performance(app: App):
    def level(kind: str, mx: int, title: str):
        items = [Item("auto (dynamic)", "auto", desc="Remove the override – system/app decides.")]
        items += [Item(f"Level {i}", str(i), hint=c("█" * i + "░" * (mx - i), "accent")) for i in range(mx + 1)]
        ch = app.select(title, items)
        if ch is None:
            return
        argv = ["perf", kind, ch]
        if kind == "ffr" and ch != "auto":
            mode = app.select("Foveation mode", [
                Item("Dynamic", "dyn", desc="Level is the maximum; the system adjusts below it."),
                Item("Fixed", "static", desc="Level always applies.")])
            if mode is None:
                return
            if mode == "static":
                argv.append("--static")
        app.run(argv, title)

    def res():
        items = [Item(f"{f}× native", f"{f}x") for f in ("0.8", "1.0", "1.2", "1.4", "1.6")]
        items += [Item("Custom resolution …", "__custom"), Item("Reset", "reset")]
        ch = app.select("Eye-buffer resolution", items)
        if ch == "__custom":
            ch = term.ask("Width x height", hint="e.g. 2560x2700 (per eye)")
        if ch:
            app.run(["perf", "res", ch], "Resolution")

    def hz():
        ch = app.select("Refresh rate", [Item(f"{r} Hz", r) for r in ("72", "80", "90", "120")] + [Item("auto", "auto")])
        if ch:
            app.run(["perf", "hz", ch], "Refresh rate")

    def preset():
        from .commands.perf import PRESETS
        ch = app.select("Preset", [Item(n.capitalize(), n, desc=p[4]) for n, p in PRESETS.items()])
        if ch:
            app.run(["perf", "preset", ch], f"Preset {ch}")

    app.submenu("Performance", [
        ("1", "Current values", "", "CPU/GPU level, foveation, eye buffer, refresh rate.",
         lambda: app.run(["perf", "show"], "Performance")),
        ("2", "Preset", "battery … max", "Ready-made combinations of clock levels and foveation.", preset),
        ("3", "CPU level", "0–5", "Fixed CPU clock or dynamic.", lambda: level("cpu", 5, "CPU level")),
        ("4", "GPU level", "0–5", "Fixed GPU clock or dynamic.", lambda: level("gpu", 5, "GPU level")),
        ("5", "Foveation (FFR)", "0–4", "Render the edges of the image at lower resolution – saves GPU.",
         lambda: level("ffr", 4, "Foveation")),
        ("6", "Resolution", "eye buffer", "Render resolution per eye. Higher = sharper, but more GPU load.", res),
        ("7", "Refresh rate", "72/90/120 Hz", "Force the display refresh rate.", hz),
        ("8", "Reset all", "", "Remove all performance overrides.",
         lambda: app.run(["perf", "reset"], "Reset")),
        ("9", "Processes", "CPU/RAM", "Processes by CPU load (measured over 1 second).",
         lambda: app.run(["perf", "procs"], "Processes")),
        ("m", "Live monitor", "Ctrl+C stops", "Continuous display of CPU, RAM and temperatures.",
         lambda: app.run(["perf", "monitor"], "", pause=False)),
    ])


# ---------------------------------------------------------------------- 4 Headset

def headset(app: App):
    def toggle(cmd: str, title: str, warn: str = ""):
        ch = app.select(title, [Item("On", "on"), Item("Off", "off", desc=warn)])
        if ch:
            app.run(["quest", cmd, ch], title)

    def reboot():
        ch = app.select("Reboot", [Item("Normal", ""), Item("Bootloader", "bootloader"),
                                   Item("Recovery", "recovery")])
        if ch is not None:
            app.run(["quest", "reboot"] + ([ch] if ch else []), "Reboot")

    def volume():
        v = term.ask("Volume 0–15 (empty = show)")
        app.run(["quest", "volume"] + ([v] if v else []), "Volume")

    def key():
        from .commands.quest import KEYS
        ch = app.select("Send key", [Item(k, k, hint=c(v, "grey")) for k, v in KEYS.items()])
        if ch:
            app.run(["quest", "key", ch], "Key", pause=False)

    def text():
        t = term.ask("Text for the active input field")
        if t:
            app.run(["quest", "text", t], "Text input")

    def url():
        u = term.ask("URL")
        if u:
            app.run(["quest", "open", u], "Browser")

    app.submenu("Headset", [
        ("1", "FPS overlay", "on/off", "Performance HUD in the headset.", lambda: toggle("hud", "FPS overlay")),
        ("2", "Guardian", "on/off", "Play-area boundary.", lambda: toggle(
            "guardian", "Guardian", "Without Guardian there is no warning about walls and furniture.")),
        ("3", "Proximity sensor", "on/off", "Off = display stays on without wearing the headset (until reboot).",
         lambda: toggle("proximity", "Proximity sensor")),
        ("4", "Phone SDK mode", "on/off", "For developer apps.", lambda: toggle("phonesdk", "Phone SDK")),
        ("5", "Throttle SDK", "on/off", "Less SDK polling, saves battery.", lambda: toggle("slowsdk", "Slow SDK")),
        ("6", "Restart VR shell", "", "Helps with stuck panels or menus.",
         lambda: app.run(["quest", "shell-restart"], "VR shell")),
        ("7", "Reboot", "", "Normal, bootloader or recovery.", reboot),
        ("8", "Volume", "0–15", "Set or show the media volume.", volume),
        ("9", "Send key", "Home, Back, …", "Send a key press to the headset.", key),
        ("t", "Enter text", "", "Type text into the active input field.", text),
        ("u", "Open URL", "", "Open a web page in the Quest browser.", url),
    ])


# ---------------------------------------------------------------------- 5 Apps

def apps_menu(app: App):
    def browse(flt: str):
        from .commands.apps import collect_apps
        dev = app.guard(app.dev)
        if not dev:
            return
        app.busy("Reading apps, versions and storage usage …")
        data = app.guard(collect_apps, dev)
        if data is None:
            return
        if flt == "user":
            data = [a for a in data if not a["system"]]
        elif flt == "disabled":
            data = [a for a in data if a["disabled"]]
        elif flt == "quest":
            data = [a for a in data if a["package"].startswith(("com.oculus.", "com.meta.", "horizon"))]
        data.sort(key=lambda a: -((a["apk"] or 0) + (a["data"] or 0)))
        total = sum((a["apk"] or 0) + (a["data"] or 0) for a in data)
        items = [Item(a["package"], a["package"],
                      hint=f"{ui.human_bytes((a['apk'] or 0) + (a['data'] or 0)):>9}  " + c((a["version"] or "")[:22], "grey")
                      + (c("  off", "yellow") if a["disabled"] else ""),
                      desc=f"Version {a['version']} · App {ui.human_bytes(a['apk'])} · Data {ui.human_bytes(a['data'])}"
                           f" · Cache {ui.human_bytes(a['cache'])} · updated {a['updated'] or '?'}")
                 for a in data]
        idx = 0
        while True:
            pkg = app.select("Apps", items, subtitle=f"{len(items)} · {ui.human_bytes(total)} · by size", start=idx)
            if not pkg:
                return
            idx = next((i for i, it in enumerate(items) if it.value == pkg), 0)
            app_actions(app, pkg)

    def install():
        p = term.ask("APK/OBB file(s) or game folder",
                     hint="Drag files or a whole folder into the window; separate several with ;  "
                          "OBB files go to /sdcard/Android/obb/<package>/ automatically")
        if p:
            files = [x.strip().strip('"') for x in p.split(";") if x.strip()]
            app.run(["apps", "install", *files], "Install")

    app.submenu("Apps", [
        ("1", "My apps", "user-installed", "List sorted by storage size – pick an app for all actions.",
         lambda: browse("user")),
        ("2", "All apps", "incl. system", "All installed packages.", lambda: browse("all")),
        ("3", "Meta/Quest apps", "", "Only com.oculus.*, com.meta.*, horizon*.", lambda: browse("quest")),
        ("4", "Disabled apps", "", "All disabled packages.", lambda: browse("disabled")),
        ("5", "Install app (APK + OBB)", "files or a game folder", "Install APK files (split APKs too). OBB data "
         "files in the same folder are copied to /sdcard/Android/obb/<package>/ automatically.", install),
        ("6", "Storage & cleanup", "caches, unused games", "Where the space goes, clear app caches, and remove "
         "games you have not played for a while - with a save-game backup first.", lambda: storage_menu(app)),
    ])


def app_actions(app: App, pkg: str):
    def backup():
        app.run(["backup", "create", pkg], "App backup")

    def perm(action: str):
        perm_ = term.ask("Permission", hint="e.g. RECORD_AUDIO or android.permission.CAMERA")
        if perm_:
            app.run(["apps", action, pkg, perm_], "Permission")

    app.submenu(pkg, [
        ("1", "Details & permissions", "", "Version, SDK, install source, APK paths, permissions.",
         lambda: app.run(["apps", "info", pkg], pkg)),
        ("2", "Launch", "", "Open the app.", lambda: app.run(["apps", "launch", pkg], "Launch")),
        ("3", "Stop", "force-stop", "Stop the app immediately.", lambda: app.run(["apps", "stop", pkg], "Stop")),
        ("4", "Backup with save game", "APK + data + obb", "Backs up the APK and /sdcard/Android/data|obb.", backup),
        ("5", "Export APK", "", "Copy the APK(s) to ~/Quest/apks.",
         lambda: app.run(["apps", "export", pkg], "Export")),
        ("6", "Logcat for this app", "Ctrl+C stops", "Live log from this app only (it must be running).",
         lambda: app.run(["logcat", "-p", pkg], "", pause=True)),
        ("7", "Grant permission", "", "Grant a runtime permission.", lambda: perm("grant")),
        ("8", "Revoke permission", "", "Revoke a runtime permission.", lambda: perm("revoke")),
        ("9", "Clear data", "irreversible", "Deletes the app's save games, login and settings.",
         lambda: app.run(["apps", "clear", pkg], "Clear data")),
        ("0", "Uninstall", "", "System apps only for user 0 (reversible).",
         lambda: app.run(["apps", "uninstall", pkg], "Uninstall")),
    ])


# ---------------------------------------------------------------------- 6 Backups

def _backup_items(kind: str) -> list[Item]:
    base = transfer.default_dir("backups")
    import json
    items = []
    for d in sorted(base.iterdir(), reverse=True):
        f = d / "manifest.json"
        if not f.is_file():
            continue
        try:
            m = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if m.get("kind", "apps") != kind:
            continue
        when = m.get("created", "")[:16].replace("T", " ")
        if kind == "settings":
            hint = c(when, "grey") + "  " + (m.get("message") or "") + c(f"  OS {m.get('os', '?')}", "grey")
        else:
            hint = c(when, "grey") + f"  {len(m.get('packages', {}))} apps"
        items.append(Item(d.name, str(d), hint=hint, desc=f"{d}  ·  {m.get('model', '')} {m.get('serial', '')}"))
    return items


def backups(app: App):
    def settings_backup():
        msg = term.ask("Note for the backup (optional)", default="")
        app.run(["settings", "backup"] + (["-m", msg] if msg else []), "Settings backup")

    def choose_settings(title: str):
        items = _backup_items("settings")
        if not items:
            app.message("No settings backup yet.", "info")
            return None
        return app.select(title, items)

    def diff():
        b = choose_settings("Compare with backup")
        if b:
            app.run(["settings", "diff", b], "Settings diff")

    def restore():
        b = choose_settings("Restore backup")
        if not b:
            return
        parts = [Item(p, p, checked=True, desc=d) for p, d in (
            ("global", "System-wide settings (network, power, …)"),
            ("secure", "Secure settings (Guardian, accessibility, …)"),
            ("system", "Display, sound, brightness"),
            ("packages", "Enabled/disabled state of packages"),
            ("props", "debug.oculus.* performance values"))]
        sel = app.select("What to restore?", parts, multi=True)
        if not sel:
            return
        mode = app.select("Mode", [
            Item("Dry run", "dry", desc="Only show the differences."),
            Item("Restore", "go", desc="Apply the differences (asks first, reversible via Undo)."),
            Item("Exact restore", "exact", desc="Also delete entries that did not exist in the backup.")])
        if not mode:
            return
        argv = ["settings", "restore", b, "--only", ",".join(sel)]
        argv += ["-n"] if mode == "dry" else ["--exact"] if mode == "exact" else []
        app.run(argv, "Restore")

    def app_backup():
        from .commands.apps import collect_apps
        dev = app.guard(app.dev)
        if not dev:
            return
        app.busy("Reading apps …")
        data = app.guard(collect_apps, dev)
        if data is None:
            return
        data = sorted([a for a in data if not a["system"]], key=lambda a: a["package"])
        items = [Item(a["package"], a["package"], hint=c(f"{ui.human_bytes((a['apk'] or 0) + (a['data'] or 0)):>9}", "grey"))
                 for a in data]
        sel = app.select("Back up apps", items, multi=True, subtitle="Space to mark, Enter to back up")
        if sel:
            obb = app.select("Include OBB files?", [Item("Yes (complete, often large)", "yes"),
                                                    Item("No (APK + save game only)", "no")])
            if obb:
                app.run(["backup", "create", *sel] + (["--no-obb"] if obb == "no" else []), "App backup")

    def app_restore():
        items = _backup_items("apps")
        if not items:
            return app.message("No app backup yet.", "info")
        b = app.select("Restore app backup", items)
        if b:
            app.run(["backup", "restore", b], "Restore")

    def browse_settings():
        q = term.ask("Search term (empty = all)", default="")
        app.run(["settings", "list"] + (["-q", q] if q else []), "Settings")

    def open_folder():
        p = transfer.default_dir("backups")
        if hasattr(os, "startfile"):
            os.startfile(p)
        else:
            app.message(str(p))

    app.submenu("Backups & Settings", [
        ("1", "Back up settings", "all in one file", "Back up all settings (global/secure/system), "
         "disabled packages and debug.oculus values.", settings_backup),
        ("2", "Compare settings", "diff", "What has changed since a backup?", diff),
        ("3", "Restore settings", "selection + dry run", "Pick sections, review the differences, "
         "restore. Connection settings are left untouched.", restore),
        ("4", "Back up apps", "with save games", "APK + /sdcard/Android/data + obb of the selected apps.", app_backup),
        ("5", "Restore app backup", "", "Installs the apps and restores their save games.", app_restore),
        ("6", "All backups", "", "List of all backups.", lambda: app.run(["backup", "list"], "Backups")),
        ("7", "Browse settings", "", "Show/search all settings entries.", browse_settings),
        ("8", "Open backup folder", "", str(transfer.default_dir("backups")), open_folder),
    ])


# ---------------------------------------------------------------------- 7 Media

def media(app: App):
    def shot(argv):
        app.run(["shot", *argv], "Screenshot")

    def record():
        t = app.select("Recording length", [Item(f"{s} seconds", s) for s in ("15", "30", "60", "120", "180")])
        if t:
            app.run(["record", "-t", t], "Recording")

    def logcat():
        ch = app.select("Logcat", [
            Item("Everything (live)", []), Item("Warnings and errors only", ["-l", "W"]),
            Item("Errors only", ["-l", "E"]), Item("Crashes", ["--crash", "-d"]),
            Item("With search term …", "__grep")])
        if ch == "__grep":
            g = term.ask("Regex/search term")
            ch = ["-g", g] if g else None
        if ch is not None:
            app.run(["logcat", *ch], "")

    def mirror():
        eye = app.select("Live view", [
            Item("Left eye", "left", desc="Looks like a normal screen (recommended)."),
            Item("Right eye", "right"),
            Item("Both eyes", "both", desc="Full side-by-side stereo panel."),
            Item("Left eye + record to file", "rec", desc="Saves an .mp4 to the videos folder while showing it.")])
        if not eye:
            return
        argv = ["mirror", "--eye", "left" if eye == "rec" else eye]
        if eye == "rec":
            import time as _t
            argv += ["--record", str(transfer.default_dir("videos") / f"mirror_{_t.strftime('%Y-%m-%d_%H-%M-%S')}.mp4")]
        app.run(argv, "Live view")

    def open_dir(kind):
        p = transfer.default_dir(kind)
        if hasattr(os, "startfile"):
            os.startfile(p)

    app.submenu("Media", [
        ("1", "Screenshot", "raw image of both panels", "Image of both displays (headset must be awake).",
         lambda: shot([])),
        ("2", "Screenshot, wake headset", "--wake", "Briefly wakes the headset if it is asleep.",
         lambda: shot(["--wake"])),
        ("3", "System screenshot", "one eye", "Like the Quest menu: one eye, undistorted.",
         lambda: shot(["--meta", "--wake"])),
        ("4", "Record screen", "up to 180 s", "Record a video and copy it to the PC.", record),
        ("m", "Live view on the PC", "scrcpy", "Shows the headset picture in a window on the PC (needs scrcpy). "
         "One eye by default; the display stays on without wearing the headset.", mirror),
        ("5", "Logcat", "live, filtered", "Colored system log, Ctrl+C stops.", logcat),
        ("6", "Open screenshot folder", "", str(transfer.default_dir("screenshots")),
         lambda: open_dir("screenshots")),
        ("7", "Open video folder", "", str(transfer.default_dir("videos")), lambda: open_dir("videos")),
    ])


# ---------------------------------------------------------------------- 8 Files

def files_menu(app: App):
    def browser(path="/sdcard"):
        from .commands.files import list_dir
        dev = app.guard(app.dev)
        if not dev:
            return
        while True:
            entries = app.guard(list_dir, dev, path)
            if entries is None:
                return
            items = [Item(c("▲ ..", "accent"), "..")] if path != "/" else []
            for e in entries:
                if e["type"] == "dir":
                    items.append(Item(c("► ", "accent") + e["name"] + "/", ("dir", e["name"]), hint=c(e["date"], "grey")))
                else:
                    items.append(Item("  " + e["name"], ("file", e["name"]),
                                      hint=f"{ui.human_bytes(e['size']):>9}  " + c(e["date"], "grey"),
                                      search=e["name"]))
            ch = app.select(path, items, subtitle=f"{len(entries)} entries · Enter opens",
                            start=1 if len(items) > 1 and items[0].value == ".." else 0)
            if ch is None:
                return
            if ch == "..":
                path = os.path.dirname(path.rstrip("/")) or "/"
            elif ch[0] == "dir":
                path = path.rstrip("/") + "/" + ch[1]
            else:
                remote = path.rstrip("/") + "/" + ch[1]
                act = app.select(ch[1], [Item("Copy to PC", "pull", desc="To ~/Quest/downloads"),
                                         Item("Delete", "rm", desc="Asks first")])
                if act == "pull":
                    app.run(["files", "pull", remote, str(transfer.default_dir("downloads"))], "Download")
                elif act == "rm":
                    app.run(["files", "rm", remote], "Delete")

    def upload():
        p = term.ask("File or folder on the PC", hint="Enter a path or drag it into the window")
        if p:
            dest = term.ask("Destination on the headset", default="/sdcard/Download/")
            app.run(["files", "push", p, dest], "Upload")

    def du():
        p = term.ask("Path", default="/sdcard/Android/data")
        if p:
            app.run(["files", "du", p], "Storage usage")

    app.submenu("Files", [
        ("1", "Browse", "/sdcard", "Walk through folders with the arrow keys, download or delete files.", browser),
        ("2", "Downloads", "/sdcard/Download", "", lambda: browser("/sdcard/Download")),
        ("3", "Screenshots on the headset", "", "", lambda: browser("/sdcard/Oculus/Screenshots")),
        ("4", "Videos on the headset", "", "", lambda: browser("/sdcard/Oculus/VideoShots")),
        ("5", "Upload", "PC → headset", "Copy a file or folder to the headset.", upload),
        ("6", "Storage usage", "largest folders", "", du),
    ])


# ---------------------------------------------------------------------- 9 Connection

def connection(app: App):
    def connect_ip():
        ip = term.ask("IP address of the headset", default=store.config().get("last_wifi", ""))
        if ip:
            app.run(["wifi", "connect", ip], "Connect Wi-Fi")

    def pair():
        ep = term.ask("IP:port from 'Pair device with pairing code'")
        code = term.ask("6-digit code") if ep else None
        if ep and code:
            app.run(["wifi", "pair", ep, code], "Pairing")

    def keep():
        on = bool(store.config().get("wifi_keep"))
        ch = app.select("Keep Wi-Fi ADB on", [Item("On", "on"), Item("Off", "off")],
                        subtitle=f"currently {'on' if on else 'off'}", start=0 if on else 1)
        if ch:
            app.run(["wifi", "keep", ch], "Keep Wi-Fi ADB on")

    def choose():
        devs = [d for d in list_devices()]
        if not devs:
            return app.message("No device connected.", "warn")
        items = [Item("Automatic", None, desc="Prefers USB, otherwise the only device.")]
        items += [Item(d.serial, d.serial, hint=c(f"{d.model}  {'Wi-Fi' if d.wifi else 'USB'}  {d.state}", "grey"))
                  for d in devs]
        ch = app.select("Select device", items)
        app.serial = ch
        app.refresh()

    app.submenu("Connection", [
        ("1", "Set up Wi-Fi ADB", "automatic via USB", "Enables ADB over Wi-Fi and connects "
         "(needed once via USB after every headset reboot).", lambda: app.run(["wifi", "connect"], "Wi-Fi ADB")),
        ("2", "Connect by IP", "", "Connect to a known address.", connect_ip),
        ("3", "Disconnect Wi-Fi", "", "Close all Wi-Fi connections.", lambda: app.run(["wifi", "disconnect"], "Disconnect")),
        ("4", "Show IP", "", "", lambda: app.run(["wifi", "ip"], "IP")),
        ("5", "Pairing (Android 11+)", "no USB", "Wireless debugging with a pairing code.", pair),
        ("6", "Select device", "", "When several devices are connected.", choose),
        ("7", "Show devices", "", "", lambda: app.run(["devices"], "Devices")),
        ("f", "Find headset on the network", "LAN scan", "Scans your local network for headsets with Wi-Fi ADB "
         "enabled and connects to them - useful when the IP address changed.",
         lambda: app.run(["wifi", "find"], "Find headset")),
        ("k", "Keep Wi-Fi ADB on", "auto re-enable", "Whenever the headset is connected by USB and Wi-Fi ADB is off "
         "(e.g. after a reboot), switch it back on automatically.", keep),
    ])


# ---------------------------------------------------------------------- 0 Tools

def storage_menu(app: App):
    def clean():
        mode = app.select("Clear app caches", [
            Item("Clear now", "go", desc="Removes temporary cache files of all apps. App data, logins and save "
                                         "games are not touched."),
            Item("Dry run", "dry")])
        if mode:
            app.run(["storage", "clean"] + (["-n"] if mode == "dry" else []), "Clear caches")

    def unused():
        from .commands.storage import _apps
        days = app.select("Not played for at least …", [Item(f"{d} days", d) for d in (30, 60, 90, 180)], start=1)
        if not days:
            return
        dev = app.guard(app.dev)
        if not dev:
            return
        app.busy("Reading apps and usage history …")
        apps = app.guard(_apps, dev)
        if apps is None:
            return
        rows = sorted([a for a in apps if a["days_unused"] is None or a["days_unused"] >= days],
                      key=lambda a: a["total"], reverse=True)
        if not rows:
            return app.message(f"Every app was used within the last {days} days.", "ok")
        items = [Item(a["package"], a["package"],
                      hint=f"{ui.human_bytes(a['total']):>9}  " + c(f"last played {a['last_used'] or 'unknown'}", "grey"))
                 for a in rows]
        total = sum(a["total"] for a in rows)
        sel = app.select(f"Not played for {days}+ days", items, multi=True,
                         subtitle=f"{len(rows)} apps · {ui.human_bytes(total)} · Space to mark, Enter to continue")
        if not sel:
            return
        act = app.select(f"{len(sel)} app(s)", [
            Item("Back up (with save game), then uninstall", "backup",
                 desc="Saves APK + /sdcard/Android/data + obb to ~/Quest/backups, then removes the apps. "
                      "Restore later via Backups & Settings."),
            Item("Uninstall without backup", "plain", desc="Save games stored on the headset are lost."),
            Item("Only back up", "only")])
        if not act:
            return
        if act in ("backup", "only"):
            rc = app.run(["backup", "create", *sel], "Backup", pause=(act == "only"))
            if act == "only":
                return
            if rc != 0:
                return app.message("Backup failed - nothing was uninstalled.", "err")
        for pkg in sel:
            app.run(["-y", "apps", "uninstall", pkg], f"Uninstall {pkg}", pause=False, refresh=False)
        term.wait_key()
        app.refresh()

    app.submenu("Storage & cleanup", [
        ("1", "Overview", "", "Free space, what uses it and the biggest apps with their last use.",
         lambda: app.run(["storage"], "Storage")),
        ("2", "Clear app caches", "safe", "Frees cache files of all apps - nothing important is deleted.", clean),
        ("3", "Games not played for a while", "backup + uninstall", "Pick old games, back up their save "
         "games and remove them in one go.", unused),
    ])


def modes_menu(app: App):
    from .commands.mode import BUILTIN, _state, all_modes, matches
    while True:
        dev = app.guard(app.dev)
        if not dev:
            return
        st = app.guard(_state, dev)
        if st is None:
            return
        modes = all_modes()
        items = [Item(n, ("apply", n), hint=(c("● active  ", "green") if matches(m, st) else "          ")
                      + c(("custom" if m.get("custom") else "built-in"), "grey"), desc=m.get("description", ""))
                 for n, m in modes.items()]
        items += [Item("Save current settings as a mode …", ("save", None),
                       desc="Stores the current performance values and network state under a name."),
                  Item("Delete one of my modes …", ("delete", None))]
        ch = app.select("Modes", items, subtitle="Enter switches · one undo reverts a whole switch")
        if not ch:
            return
        kind, name = ch
        if kind == "apply":
            app.run(["mode", "apply", name], f"Mode {name}")
        elif kind == "save":
            n = term.ask("Name for the mode", hint="letters, digits, - and _")
            if n:
                msg = term.ask("Description (optional)", default="")
                app.run(["mode", "save", n] + (["-m", msg] if msg else []), "Save mode")
        else:
            own = [n for n, m in modes.items() if m.get("custom")]
            if not own:
                app.message("You have no custom modes yet.")
                continue
            d = app.select("Delete mode", [Item(n, n) for n in own])
            if d:
                app.run(["mode", "delete", d], "Delete mode")


def repair_menu(app: App):
    def run_fix(kind: str, title: str):
        mode = app.select(title, [
            Item("Dry run - show the steps", "dry", desc="Shows what would be changed. Nothing is touched."),
            Item("Repair now", "go", desc="Runs the fix. Package changes are journaled and can be undone.")])
        if mode:
            app.run(["repair", kind] + (["-n"] if mode == "dry" else []), title)

    app.submenu("Repair", [
        ("1", "Check for problems", "packages, logs, services", "Looks for disabled store/panel packages, "
         "offline mode, known error lines in the system log and dead services. Run it right after a problem.",
         lambda: app.run(["repair", "check"], "Repair check")),
        ("2", "Store purchases & installs", "re-enable + reboot", "Purchased apps gone, listed under 'Unknown "
         "sources' or 'not purchased', Store apps will not install. Re-enables ocms, store and "
         "credentialsmanager, turns offline mode off if needed, refreshes the library and reboots - the purchase "
         "sync only runs at boot.", lambda: run_fix("store", "Repair Store")),
        ("3", "Home panels missing", "enable ocms + restart shell", "Only passthrough and the system bar, no home, "
         "library or universal menu ('Placement request Timeout'). Enables com.oculus.ocms and restarts the VR shell.",
         lambda: run_fix("panels", "Repair panels")),
        ("4", "After a firmware update", "re-disable packages", "Horizon OS updates switch disabled packages "
         "back on. Restores the state from before the update (settings snapshot first).",
         lambda: app.offer_update_restore(always=True)),
        ("5", "Controllers / tracking dead", "reboot", "The tracking service crashed and cannot be restarted from "
         "adb - a reboot brings 6DoF and the controllers back.", lambda: app.run(["repair", "tracking"], "Repair tracking")),
    ])


def tools_menu(app: App):
    def shell():
        term.clear()
        app.run(["shell"], "adb shell (exit to quit)", pause=False)

    def run_saved():
        from .commands.tools import _all_cmds
        items = [Item(x["name"], x["name"], hint=c(x["command"], "cyan"), desc=x.get("note", "")) for x in _all_cmds()]
        ch = app.select("Command library", items)
        if ch:
            app.run(["cmd", "run", ch], ch)

    def add_saved():
        name = term.ask("Name")
        cmd = term.ask("adb command", hint='e.g. shell dumpsys battery') if name else None
        if name and cmd:
            app.run(["cmd", "add", name, cmd], "Save command")

    def undo():
        dev = app.guard(app.dev)
        if not dev:
            return
        bs = [b for b in store.batches(dev.hwid) if not b["undone"]]
        if not bs:
            return app.message("Nothing to undo.")
        items = [Item(b["label"], b["batch"], hint=c(b["ts"][:16].replace("T", " ") + f"  {len(b['entries'])} change(s)", "grey"),
                      desc=" · ".join(f"{e['key']}: {e['before']} → {e['after']}" for e in b["entries"][:4]))
                 for b in reversed(bs)]
        ch = app.select("Undo", items, subtitle="newest first")
        if ch:
            app.run(["undo", ch], "Undo")

    def adb_path():
        p = term.ask("Path to adb.exe", default=store.config().get("adb_path", ""))
        if p:
            app.run(["config", "set", "adb_path", p], "Configuration")

    def raw_cmd():
        line = term.ask("Quest Tools command", hint="e.g. perf cpu 3   ·   debloat scan -q oculus")
        if line:
            import shlex
            try:
                argv = [t[1:-1] if len(t) > 1 and t[0] == t[-1] and t[0] in "'\"" else t
                        for t in shlex.split(line, posix=(os.name != "nt"))]
            except ValueError as e:
                return app.message(f"Cannot parse the command: {e}", "err")
            app.run(argv, line)

    app.submenu("Tools", [
        ("1", "adb shell", "interactive", "Direct shell on the headset.", shell),
        ("2", "Command library", "run", "Saved and built-in commands.", run_saved),
        ("3", "Save command", "", "Add your own adb command to the library.", add_saved),
        ("4", "Change log", "", "All recorded changes.", lambda: app.run(["history", "-n", "30"], "History")),
        ("5", "Undo", "pick", "Revert a specific earlier change.", undo),
        ("6", "Enter command", "expert mode", "Run any Quest Tools command (as on the command line).",
         raw_cmd),
        ("7", "Configuration", "", "", lambda: app.run(["config", "show"], "Configuration")),
        ("8", "Set adb path", "", "In case adb is not found automatically.", adb_path),
        ("s", "Install / update scrcpy", "for the live view", "Downloads the latest scrcpy from GitHub "
         "(checksum verified) into Quest Tools' folder. Only needed for Media → Live view.",
         lambda: app.run(["install-scrcpy", "--update"], "Install scrcpy")),
        ("9", "Help for all commands", "", "Overview of all command-line commands.", lambda: app.run(["--help"], "Help")),
    ])


def start(serial: str | None = None) -> int:
    if not sys.stdin.isatty():
        print("The interface needs an interactive terminal. Commands: questtools --help")
        return 2
    app = App(serial)
    try:
        app.loop()
    except KeyboardInterrupt:
        term.clear()
    finally:
        term.show_cursor()
    return 0
