"""shell, cmd (saved commands), history, undo, config."""

from __future__ import annotations

import argparse
import shlex
import subprocess

from .. import ops, store, ui
from ..adb import AdbError, find_adb
from ..ui import c
from . import add, group

BUILTIN_COMMANDS = [
    {"name": "battery", "command": "shell dumpsys battery", "note": "raw battery status"},
    {"name": "controller", "command": "shell dumpsys OVRRemoteService | grep 'Paired device'",
     "note": "controllers incl. battery/firmware"},
    {"name": "thermals", "command": "shell dumpsys thermalservice", "note": "all temperature sensors"},
    {"name": "focus", "command": "shell dumpsys activity activities | grep -m1 ResumedActivity",
     "note": "foreground app"},
    {"name": "user-apps", "command": "shell pm list packages -3", "note": "user-installed apps"},
    {"name": "ip", "command": "shell ip route get 1", "note": "Wi-Fi IP"},
    {"name": "uptime", "command": "shell uptime", "note": "uptime/load"},
]


def register(sub, common):
    p = add(sub, common, "shell", "adb shell (interactive or a single command)", cmd_shell, aliases=["sh"])
    p.add_argument("command", nargs=argparse.REMAINDER, help="Command for the device shell (flags are passed through)")

    _, g = group(sub, common, "cmd", "Saved commands (your own library)")
    add(g, common, "list", "All saved commands", cmd_cmd_list, aliases=["ls"])
    p = add(g, common, "add", "Save a command, e.g.: questtools cmd add fps \"shell getprop debug.oculus.fpsCounter\"",
            cmd_cmd_add)
    p.add_argument("name")
    p.add_argument("command", help='adb command, e.g. "shell dumpsys battery"')
    p.add_argument("--note", default="")
    p = add(g, common, "run", "Run a saved command", cmd_cmd_run)
    p.add_argument("name")
    p = add(g, common, "rm", "Delete a saved command", cmd_cmd_rm)
    p.add_argument("name")

    p = add(sub, common, "history", "Show the change journal", cmd_history)
    p.add_argument("-n", type=int, default=15, help="Number of entries")
    p.add_argument("--all-devices", action="store_true")
    p = add(sub, common, "undo", "Undo a change (default: the last one)", cmd_undo)
    p.add_argument("batch", nargs="?", help="Batch ID from questtools history")

    _, g = group(sub, common, "config", "questtools settings (adb_path, default_serial …)")
    add(g, common, "show", "Show configuration", cmd_config_show)
    p = add(g, common, "set", "Set a value", cmd_config_set)
    p.add_argument("key", choices=["adb_path", "scrcpy_path", "default_serial", "last_wifi"])
    p.add_argument("value")
    p = add(g, common, "unset", "Remove a value", cmd_config_unset)
    p.add_argument("key")


def cmd_shell(ctx):
    dev = ctx.dev
    if not ctx.args.command:
        ui.info(f'Interactive shell on {dev} - "exit" quits.')
        return subprocess.call([find_adb(), "-s", dev.serial, "shell"])
    r = dev.sh(" ".join(ctx.args.command), timeout=None)
    if ctx.emit({"code": r.code, "stdout": r.out, "stderr": r.err}):
        return r.code
    print(r.out, end="")
    if r.err:
        print(c(r.err, "red"), end="")
    return r.code


def _all_cmds() -> list[dict]:
    own = store.commands()
    names = {x["name"] for x in own}
    return own + [dict(b, builtin=True) for b in BUILTIN_COMMANDS if b["name"] not in names]


def cmd_cmd_list(ctx):
    cmds = _all_cmds()
    if ctx.emit(cmds):
        return
    ui.banner("Command library", f"{len(cmds)} commands")
    ui.table(["Name", "Command", "Note"],
             [[c(x["name"], "bold") + (c(" ·", "grey") if x.get("builtin") else ""), c(x["command"], "cyan"),
               c(x.get("note", ""), "grey")] for x in cmds])
    ui.hint("· = built-in · run: questtools cmd run <name>")


def cmd_cmd_add(ctx):
    command = ctx.args.command.strip()
    if command.startswith("adb "):
        command = command[4:].strip()
    if not command or command == "adb":
        raise AdbError("Empty command.", 'Example: questtools cmd add bat "shell dumpsys battery"')
    if command.split()[0] != "shell":
        try:
            shlex.split(command)
        except ValueError as e:
            raise AdbError(f"Cannot parse the command: {e}")
    cmds = [x for x in store.commands() if x["name"] != ctx.args.name]
    cmds.append({"name": ctx.args.name, "command": command, "note": ctx.args.note, "created": store.now_iso()})
    store.save_commands(cmds)
    ui.ok(f"Saved: {ctx.args.name}")


def cmd_cmd_run(ctx):
    item = next((x for x in _all_cmds() if x["name"] == ctx.args.name), None)
    if not item:
        raise AdbError(f'Unknown command "{ctx.args.name}".', "List: questtools cmd list")
    toks = item["command"].split(" ", 1)
    dev = ctx.dev
    ui.out(c("$ ", "grey") + c(f"adb -s {dev.serial} {item['command']}", "cyan"))
    if toks[0] == "shell":
        r = dev.sh(toks[1] if len(toks) > 1 else "", timeout=None)
    else:
        r = dev.run(*shlex.split(item["command"]), timeout=None)
    print(r.out, end="")
    if r.err:
        print(c(r.err, "red"), end="")
    return r.code


def cmd_cmd_rm(ctx):
    cmds = store.commands()
    left = [x for x in cmds if x["name"] != ctx.args.name]
    if len(left) == len(cmds):
        raise AdbError(f'"{ctx.args.name}" is not one of your saved commands.')
    store.save_commands(left)
    ui.ok(f"Deleted: {ctx.args.name}")


def _fmt(v) -> str:
    if v is None:
        return c("(empty)", "grey")
    return str(v) if v != "" else c('""', "grey")


def cmd_history(ctx):
    serial = None if ctx.args.all_devices else ctx.dev.hwid
    bs = store.batches(serial)[-ctx.args.n:]
    if ctx.emit(bs):
        return
    ui.banner("Change journal", serial or "all devices")
    if not bs:
        return ui.info("No changes recorded yet.")
    for b in reversed(bs):
        head = c(b["batch"], "accent") + "  " + c(b["ts"][:19].replace("T", " "), "grey") + "  " + c(b["label"], "bold")
        if b["undone"]:
            head += c("  (undone)", "grey")
        print()
        print(head)
        for e in b["entries"][:12]:
            ns = f"{e['ns']}/" if e.get("ns") else ""
            print(f"    {c(e['kind'], 'grey'):<8} {ns}{e['key']}  {_fmt(e['before'])} {c('→', 'grey')} {_fmt(e['after'])}")
        if len(b["entries"]) > 12:
            print(c(f"    … and {len(b['entries']) - 12} more", "grey"))


def cmd_undo(ctx):
    dev = ctx.dev
    bs = [b for b in store.batches(dev.hwid) if not b["undone"]]
    if not bs:
        return ui.info("Nothing to undo.")
    if ctx.args.batch:
        b = next((x for x in bs if x["batch"] == ctx.args.batch), None)
        if not b:
            raise AdbError(f"Batch {ctx.args.batch} not found or already undone.", "questtools history")
    else:
        b = bs[-1]
    ui.banner("Undo", f"{b['batch']} · {b['label']} · {len(b['entries'])} change(s)")
    for e in reversed(b["entries"]):
        ns = f"{e['ns']}/" if e.get("ns") else ""
        print(f"  {c('←', 'accent')} {ns}{e['key']}  {_fmt(e['after'])} {c('→', 'grey')} {_fmt(e['before'])}")
    if not ui.confirm("Undo?", ctx.yes, default=True):
        return 1
    fail = 0
    for e in reversed(b["entries"]):
        try:
            ops.revert(dev, e)
        except AdbError as err:
            fail += 1
            ui.err(f"{e['key']}: {err.message}")
    if fail:
        ui.warn(f"{fail} of {len(b['entries'])} changes could not be reverted. "
                f"The entry stays in the history - fix the cause and run: questtools undo {b['batch']}")
        return 1
    store.mark_undone(dev.hwid, b["batch"])
    ui.ok("Undone.")


def cmd_config_show(ctx):
    cfg = store.config()
    if ctx.emit(cfg):
        return
    ui.banner("Configuration", str(store.data_dir()))
    try:
        adb = find_adb()
    except AdbError:
        adb = c("not found", "red")
    ui.kv([("adb", adb)] + [(k, v if isinstance(v, str) else str(v)) for k, v in cfg.items() if k != "seen"])


def cmd_config_set(ctx):
    store.set_config(ctx.args.key, ctx.args.value)
    ui.ok(f"{ctx.args.key} = {ctx.args.value}")


def cmd_config_unset(ctx):
    store.set_config(ctx.args.key, None)
    ui.ok(f"{ctx.args.key} removed")
