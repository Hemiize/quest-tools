"""quest: headset toggles, reboot, proximity sensor, volume, input."""

from __future__ import annotations

import re

from .. import ops, store, ui
from ..adb import AdbError, quote
from ..ui import c
from . import add, dry, group, is_on, onoff

PROP_TOGGLES = {
    "hud": ("debug.oculus.fpsCounter", "FPS/performance overlay in the headset"),
    "phonesdk": ("debug.oculus.enablePhoneSdk", "Phone SDK mode for developer apps"),
    "slowsdk": ("debug.oculus.slowSDK", "Throttle SDK polling (saves battery)"),
}
KEYS = {"home": "KEYCODE_HOME", "back": "KEYCODE_BACK", "menu": "KEYCODE_MENU", "power": "KEYCODE_POWER",
        "wake": "KEYCODE_WAKEUP", "sleep": "KEYCODE_SLEEP", "play": "KEYCODE_MEDIA_PLAY_PAUSE",
        "enter": "KEYCODE_ENTER", "up": "KEYCODE_DPAD_UP", "down": "KEYCODE_DPAD_DOWN",
        "left": "KEYCODE_DPAD_LEFT", "right": "KEYCODE_DPAD_RIGHT", "vol+": "KEYCODE_VOLUME_UP",
        "vol-": "KEYCODE_VOLUME_DOWN", "mute": "KEYCODE_VOLUME_MUTE"}


def register(sub, common):
    _, g = group(sub, common, "quest", "Headset: overlays, Guardian, sensor, reboot, input", aliases=["q"])
    for name, (_, desc) in PROP_TOGGLES.items():
        p = dry(onoff(add(g, common, name, desc, cmd_prop_toggle)))
        p.set_defaults(toggle=name)
    p = dry(onoff(add(g, common, "guardian", "Guardian on/off (secure user_guardian; depends on firmware)",
                      cmd_guardian)))
    p = onoff(add(g, common, "proximity", "Proximity sensor: off = display stays on without wearing the headset",
                  cmd_proximity, aliases=["prox"]))
    add(g, common, "shell-restart", "Restart the VR shell (helps with frozen panels)", cmd_shell_restart)
    p = add(g, common, "reboot", "Reboot (normal, bootloader, recovery, fastboot)", cmd_reboot)
    p.add_argument("mode", nargs="?", default="", choices=["", "bootloader", "recovery", "fastboot"])
    p = add(g, common, "volume", "Set or show media volume 0–15", cmd_volume, aliases=["vol"])
    p.add_argument("level", nargs="?", type=int)
    p = add(g, common, "key", "Send a key: " + ", ".join(KEYS), cmd_key)
    p.add_argument("key")
    p = add(g, common, "text", "Type text into the active input field", cmd_text)
    p.add_argument("text")
    p = add(g, common, "open", "Open a URL in the Quest browser", cmd_open)
    p.add_argument("url")


def cmd_prop_toggle(ctx):
    key, desc = PROP_TOGGLES[ctx.args.toggle]
    on = is_on(ctx.args.state)
    batch = None if ctx.dry else store.Batch(ctx.dev.hwid, f"quest {ctx.args.toggle} {ctx.args.state}")
    r = ops.set_prop(ctx.dev, batch, key, "1" if on else "0", dry=ctx.dry)
    _report(r, f"{desc}: {'on' if on else 'off'}")
    if r == "changed" and ctx.args.toggle == "hud":
        ui.hint("Visible on the next app launch. Right away: questtools quest shell-restart")


def cmd_guardian(ctx):
    on = is_on(ctx.args.state)
    if not on:
        ui.warn("Without Guardian there is no warning about walls and furniture.")
        if not ui.confirm("Really turn off Guardian?", ctx.yes):
            return 1
    batch = None if ctx.dry else store.Batch(ctx.dev.hwid, f"quest guardian {ctx.args.state}")
    r = ops.put_setting(ctx.dev, batch, "secure", "user_guardian", "1" if on else "0", dry=ctx.dry)
    _report(r, f"Guardian: {'on' if on else 'off'}")


def cmd_proximity(ctx):
    on = is_on(ctx.args.state)
    action = "automation_disable" if on else "prox_close"
    r = ctx.dev.sh(f"am broadcast -a com.oculus.vrpowermanager.{action}")
    if "Broadcast completed" not in r.out:
        raise AdbError("Broadcast to vrpowermanager failed.", r.out + r.err)
    if on:
        ui.ok("Proximity sensor back to normal.")
    else:
        ui.ok('Proximity sensor overridden - display stays on until reboot or "questtools quest proximity on".')
        ui.hint("Handy for recordings/scrcpy without wearing the headset.")


def cmd_shell_restart(ctx):
    ctx.dev.sh_ok("am force-stop com.oculus.vrshell", "Could not stop the VR shell.")
    ui.ok("VR shell restarted (it relaunches automatically).")


def cmd_reboot(ctx):
    mode = ctx.args.mode
    label = mode or "normal"
    if mode and not ui.confirm(f'Reboot into "{mode}" mode?', ctx.yes):
        return 1
    args = ["reboot"] + ([mode] if mode else [])
    ctx.dev.run(*args, timeout=30)
    ui.ok(f"Reboot ({label}) triggered.")
    if ctx.dev.entry and ctx.dev.entry.wifi:
        ui.hint("ADB over Wi-Fi is off after the reboot - via USB: questtools wifi connect")


def cmd_volume(ctx):
    if ctx.args.level is None:
        r = ctx.dev.sh("cmd media_session volume --stream 3 --get")
        line = next((ln for ln in r.lines() if "volume is" in ln), r.text)
        ui.info(line.strip() or "Volume unknown")
        return
    lv = ctx.args.level
    if not 0 <= lv <= 15:
        raise AdbError("Volume must be 0–15.")
    ctx.dev.sh_ok(f"cmd media_session volume --stream 3 --set {lv}", "Setting the volume failed.")
    ui.ok(f"Volume: {lv}/15")


def cmd_key(ctx):
    k = ctx.args.key.lower()
    raw = ctx.args.key.upper()
    code = KEYS.get(k) or (raw if re.fullmatch(r"KEYCODE_[A-Z0-9_]+|\d{1,3}", raw) else None)
    if not code:
        raise AdbError(f'Unknown key "{ctx.args.key}".', "Known: " + ", ".join(KEYS))
    ctx.dev.sh_ok(f"input keyevent {code}", "Key press failed.")
    ui.ok(f"Key {code} sent.")


def cmd_text(ctx):
    # `input text` only understands spaces as %s
    t = ctx.args.text.replace(" ", "%s")
    ctx.dev.sh_ok(f"input text {quote(t)}", "Text input failed.")
    ui.ok("Text sent.")


def cmd_open(ctx):
    url = ctx.args.url
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    ctx.dev.sh_ok(f"am start -a android.intent.action.VIEW -d {quote(url)}", "Could not open the URL.")
    ui.ok(f"Opened: {url}")


def _report(result: str, text: str):
    if result == "changed":
        ui.ok(text)
    elif result == "unchanged":
        ui.info(text + c("  (already set)", "grey"))
    else:
        ui.info("Dry run: " + text)
