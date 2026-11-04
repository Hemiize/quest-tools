"""storage: where the space goes, clear app caches, find big apps you no longer play."""

from __future__ import annotations

import re
from datetime import datetime

from .. import ui
from ..adb import AdbError
from ..ui import c
from . import add, dry, group


def register(sub, common):
    p, g = group(sub, common, "storage", "Storage overview, clear caches, find unused apps", aliases=["space"])
    p.set_defaults(func=cmd_overview)
    add(g, common, "overview", "Free space, what uses it, biggest apps with last use", cmd_overview)
    dry(add(g, common, "clean", "Clear the cache files of all apps (no data or save games are touched)", cmd_clean))
    p = add(g, common, "unused", "User apps not played for a while, biggest first", cmd_unused)
    p.add_argument("--days", type=int, default=60, help="not used for at least this many days (default 60)")


def _df(dev) -> tuple[int, int]:
    """(free, total) bytes of user storage."""
    line = [ln for ln in dev.sh("df -k /storage/emulated/0").lines() if ln.startswith("/")]
    if not line:
        raise AdbError("Cannot read storage usage.")
    cols = line[-1].split()
    return int(cols[3]) * 1024, int(cols[1]) * 1024


def last_used(dev) -> dict[str, datetime]:
    """Most recent use per package from usage stats (daily/weekly/monthly/yearly buckets)."""
    r = dev.sh("dumpsys usagestats | grep 'lastTimeUsed='", timeout=60)
    out: dict[str, datetime] = {}
    for pkg, ts in re.findall(r'package=([\w.]+) .*?lastTimeUsed="([\d\- :]+)"', r.out):
        try:
            t = datetime.strptime(ts.strip(), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if t.year < 2000:
            continue
        if pkg not in out or t > out[pkg]:
            out[pkg] = t
    return out


def _summary(dev) -> dict:
    r = dev.sh("dumpsys diskstats | grep -E '^(App Size|App Data Size|App Cache Size|Photos Size|Videos Size|"
               "Other Size):'")
    return {k.strip(): int(v) for k, v in re.findall(r"^([\w ]+):\s*(\d+)\s*$", r.out, re.M)}


def _apps(dev) -> list[dict]:
    from .apps import collect_apps
    apps = [a for a in collect_apps(dev) if not a["system"]]
    used = last_used(dev)
    now = datetime.now()
    for a in apps:
        t = used.get(a["package"])
        a["last_used"] = t.strftime("%Y-%m-%d") if t else None
        a["days_unused"] = (now - t).days if t else None
        a["total"] = (a["apk"] or 0) + (a["data"] or 0)
    return apps


def _age(a) -> str:
    if a["days_unused"] is None:
        return c("never / unknown", "grey")
    d = a["days_unused"]
    txt = "today" if d == 0 else f"{d} days ago"
    return c(txt, "green" if d < 14 else "yellow" if d < 60 else "red")


def cmd_overview(ctx):
    dev = ctx.dev
    with ui.Spinner("Reading storage and app usage …"):
        free, total = _df(dev)
        summ = _summary(dev)
        apps = sorted(_apps(dev), key=lambda a: a["total"], reverse=True)
    if ctx.emit({"free": free, "total": total, "summary": summ, "apps": apps}):
        return
    ui.banner("Storage", str(dev))
    used = total - free
    ui.kv([("Used", f"{ui.bar(used / total, 30)} {ui.human_bytes(used)} / {ui.human_bytes(total)}"),
           ("Free", ui.human_bytes(free))])
    ui.section("What uses it")
    parts = [("Apps", summ.get("App Size", 0)), ("App data", summ.get("App Data Size", 0)),
             ("App caches", summ.get("App Cache Size", 0)), ("Photos/videos", summ.get("Photos Size", 0)
                                                             + summ.get("Videos Size", 0)),
             ("Other files", summ.get("Other Size", 0))]
    top = max([v for _, v in parts] + [1])
    for name, v in parts:
        print(f"  {ui.pad(c(name, 'grey'), 14)} {ui.bar(v / top, 24, 2, 2)} {ui.human_bytes(v):>9}")
    ui.hint("Category sizes are refreshed by the system from time to time; 'Free' is live.")
    ui.section("Biggest apps")
    ui.table(["App", "Size", "Last played"],
             [[a["package"], ui.human_bytes(a["total"]), _age(a)] for a in apps[:15]], aligns=["<", ">", "<"])
    print()
    ui.hint("Free space: questtools storage clean (caches)  ·  questtools storage unused (old games)")


def cmd_clean(ctx):
    dev = ctx.dev
    summ = _summary(dev)
    cache = summ.get("App Cache Size")
    ui.banner("Clear app caches", str(dev))
    ui.info("Removes temporary cache files of all apps. App data, logins and save games are not touched.")
    if cache:
        ui.info(f"Caches according to the last system count: about {ui.human_bytes(cache)}")
    if ctx.dry:
        return ui.info("Dry run: pm trim-caches would now be run.")
    free_before, _ = _df(dev)
    with ui.Spinner("Clearing caches …"):
        r = dev.sh("pm trim-caches 9999G", timeout=180)
    if (r.out + r.err).strip() and "Success" not in r.out:
        raise AdbError("Clearing caches failed.", (r.out + r.err).strip())
    free_after, _ = _df(dev)
    gained = max(0, free_after - free_before)
    ctx.emit({"freed": gained})
    ui.ok(f"Caches cleared - {ui.human_bytes(gained)} freed ({ui.human_bytes(free_after)} free now).")
    ui.hint("Apps rebuild some cache on their next start; that is normal.")


def cmd_unused(ctx):
    dev = ctx.dev
    with ui.Spinner("Reading apps and usage history …"):
        apps = _apps(dev)
    days = ctx.args.days
    rows = [a for a in apps if a["days_unused"] is None or a["days_unused"] >= days]
    rows.sort(key=lambda a: a["total"], reverse=True)
    if ctx.emit(rows):
        return
    total = sum(a["total"] for a in rows)
    ui.banner(f"Not played for {days}+ days", f"{len(rows)} apps · {ui.human_bytes(total)}")
    if not rows:
        return ui.ok("Every installed app was used recently.")
    ui.table(["App", "Size", "Last played", "Version"],
             [[a["package"], ui.human_bytes(a["total"]), _age(a), c((a["version"] or "")[:16], "grey")] for a in rows],
             aligns=["<", ">", "<", "<"])
    print()
    ui.hint("Back up first (keeps the save game), then uninstall: questtools backup create <pkg> · "
            "questtools apps uninstall <pkg>  - or use Apps → Storage in the menu.")
    ui.hint("'never / unknown' = no usage record (usage history only goes back a limited time).")
