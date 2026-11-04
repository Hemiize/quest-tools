"""files: file manager for /sdcard with transfer progress."""

from __future__ import annotations

import posixpath
import re
from pathlib import Path

from .. import transfer, ui
from ..adb import AdbError, quote
from ..ui import c
from . import add, group

SHORTCUTS = {"sd": "/sdcard", "dl": "/sdcard/Download", "pics": "/sdcard/Oculus/Screenshots",
             "videos": "/sdcard/Oculus/VideoShots", "movies": "/sdcard/Movies", "android": "/sdcard/Android"}


def register(sub, common):
    _, g = group(sub, common, "files", "Files on the headset: ls, pull, push, rm, mkdir, mv",
                 aliases=["f"])
    p = add(g, common, "ls", "List a folder (shortcuts: " + ", ".join(SHORTCUTS) + ")", cmd_ls)
    p.add_argument("path", nargs="?", default="/sdcard")
    p.add_argument("-a", "--all", action="store_true", help="show hidden files")
    p = add(g, common, "pull", "Copy a file/folder to the PC", cmd_pull)
    p.add_argument("remote")
    p.add_argument("local", nargs="?", default=".")
    p = add(g, common, "push", "Copy a file/folder to the headset", cmd_push)
    p.add_argument("local")
    p.add_argument("remote", nargs="?", default="/sdcard/Download/")
    p = add(g, common, "rm", "Delete (asks first)", cmd_rm)
    p.add_argument("path")
    p = add(g, common, "mkdir", "Create a folder", cmd_mkdir)
    p.add_argument("path")
    p = add(g, common, "mv", "Move/rename", cmd_mv)
    p.add_argument("src")
    p.add_argument("dst")
    p = add(g, common, "du", "Largest folders under a path", cmd_du)
    p.add_argument("path", nargs="?", default="/sdcard")


def resolve(path: str) -> str:
    # Git Bash (MSYS) rewrites /sdcard/... to C:/Program Files/Git/sdcard/... - undo that
    m = re.match(r"^[A-Za-z]:[/\\].*?[/\\]Git[/\\]((?:sdcard|storage|data|system)\b.*)$", path)
    if m:
        path = "/" + m.group(1).replace("\\", "/")
    head, _, rest = path.partition("/")
    if head in SHORTCUTS:
        path = SHORTCUTS[head] + ("/" + rest if rest else "")
    if not path.startswith("/"):
        path = "/sdcard/" + path
    return posixpath.normpath(path)


LS_RE = re.compile(r"^([\-dlcbsp][rwxsStT\-]{9})\S*\s+\d+\s+\S+\s+\S+\s+(\d+)?\s*(\d{4}-\d\d-\d\d \d\d:\d\d)\s+(.+)$")


def list_dir(dev, path: str, hidden: bool = False) -> list[dict]:
    r = dev.sh(f"ls -l{'a' if hidden else ''} {quote(path)}/")
    if "No such file" in r.out + r.err or "Permission denied" in r.out + r.err:
        raise AdbError(f"{path}: not readable.", (r.out + r.err).strip())
    entries = []
    for line in r.lines():
        m = LS_RE.match(line)
        if not m:
            continue
        perm, size, date, name = m.groups()
        if name in (".", ".."):
            continue
        kind = {"d": "dir", "l": "link"}.get(perm[0], "file")
        if kind == "link":
            name = name.split(" -> ")[0]
        entries.append({"name": name, "type": kind, "size": int(size or 0), "date": date})
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
    return entries


def cmd_ls(ctx):
    path = resolve(ctx.args.path)
    entries = list_dir(ctx.dev, path, ctx.args.all)
    if ctx.emit({"path": path, "entries": entries}):
        return
    ui.banner(path, f"{len(entries)} entries")
    rows = []
    for e in entries:
        icon = c("► ", "accent") if e["type"] == "dir" else c("→ ", "cyan") if e["type"] == "link" else "  "
        name = c(e["name"] + "/", "bold") if e["type"] == "dir" else e["name"]
        rows.append([icon + name, "" if e["type"] == "dir" else ui.human_bytes(e["size"]), c(e["date"], "grey")])
    ui.table(["Name", "Size", "Modified"], rows, aligns=["<", ">", "<"])


def cmd_pull(ctx):
    remote = resolve(ctx.args.remote)
    local = Path(ctx.args.local)
    target = transfer.pull(ctx.dev, remote, local)
    ui.ok(f"Saved: {target.resolve()}")


def cmd_push(ctx):
    remote = resolve(ctx.args.remote) + ("/" if ctx.args.remote.endswith("/") else "")
    transfer.push(ctx.dev, Path(ctx.args.local), remote)
    ui.ok(f"Copied to {remote}")


def cmd_rm(ctx):
    path = resolve(ctx.args.path)
    real = re.sub(r"^/sdcard(?=/|$)", "/storage/emulated/0", path).rstrip("/") or "/"
    protected = {"/storage/emulated/0", "/storage/emulated/0/Android", "/storage/emulated/0/Android/data",
                 "/storage/emulated/0/Android/obb", "/storage/emulated/0/Android/media"}
    if not real.startswith(("/storage/emulated/0/", "/data/local/tmp/")) or real in protected:
        raise AdbError(f"Refusing to delete {path}.",
                       "Only files and folders inside user storage (/sdcard/...) can be deleted, "
                       "and never the storage root or Android/data|obb themselves.")
    size = transfer.remote_size(ctx.dev, path)
    if size is None:
        raise AdbError(f"{path} does not exist.")
    if not ui.confirm(f"Permanently delete {path} ({ui.human_bytes(size)})?", ctx.yes):
        return 1
    ctx.dev.sh_ok(f"rm -rf {quote(path)}", f"Could not delete {path}.")
    ui.ok(f"Deleted: {path}")


def cmd_mkdir(ctx):
    path = resolve(ctx.args.path)
    ctx.dev.sh_ok(f"mkdir -p {quote(path)}", "Could not create the folder.")
    ui.ok(f"Created: {path}")


def cmd_mv(ctx):
    src, dst = resolve(ctx.args.src), resolve(ctx.args.dst)
    ctx.dev.sh_ok(f"mv {quote(src)} {quote(dst)}", "Move failed.")
    ui.ok(f"{src} → {dst}")


def cmd_du(ctx):
    path = resolve(ctx.args.path)
    with ui.Spinner(f"Measuring {path} …"):
        r = ctx.dev.sh(f"du -sk {quote(path)}/* {quote(path)}/.[!.]* 2>/dev/null | sort -rn | head -20", timeout=120)
    rows = []
    for line in r.lines():
        parts = line.split("\t", 1)
        if len(parts) == 2 and parts[0].isdigit():
            rows.append((int(parts[0]) * 1024, parts[1]))
    if ctx.emit([{"bytes": b, "path": p} for b, p in rows]):
        return
    ui.banner(f"Storage under {path}")
    top = rows[0][0] if rows else 1
    ui.table(["Size", "", "Path"],
             [[ui.human_bytes(b), ui.bar(b / top, 16, 2, 2), p.replace(path + "/", "")] for b, p in rows],
             aligns=[">", "<", "<"])
