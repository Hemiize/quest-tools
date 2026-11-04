"""Entry point and command tree of questtools."""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__, ui
from .adb import AdbError, Device, pick
from .commands import apps, backup, debloat, device, files, media, net, perf, quest, repair, settings, tools

MODULES = [device, repair, quest, perf, debloat, apps, settings, net, media, files, backup, tools]


class Ctx:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.yes: bool = getattr(args, "yes", False)
        self.dry: bool = getattr(args, "dry_run", False)
        self.json: bool = getattr(args, "json", False)
        self._dev: Device | None = None

    @property
    def dev(self) -> Device:
        if self._dev is None:
            with ui.Spinner("Looking for device …"):
                self._dev = pick(self.args.serial)
        return self._dev

    def emit(self, data) -> bool:
        """Prints data in --json mode. True = output handled."""
        if self.json:
            print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
            return True
        return False


EPILOG = f"""\
{ui.c('Quick start', 'accent', 'bold')}
  questtools status              Overview of the headset
  questtools doctor              Health check (incl. drift after a firmware update)
  questtools debloat check       Check the current state against the 'privacy' profile
  questtools debloat apply privacy --dry-run
  questtools perf show           CPU/GPU/FFR/resolution
  questtools undo                Undo the last change

Every change is logged with its previous value (questtools history / questtools undo).
"""


def build_parser() -> argparse.ArgumentParser:
    def make_common(suppress: bool) -> argparse.ArgumentParser:
        # Subcommands must not overwrite the global values with their defaults.
        d = {"default": argparse.SUPPRESS} if suppress else {}
        cp = argparse.ArgumentParser(add_help=False)
        cp.add_argument("-s", "--serial", help="Target device (serial or ip:port)", **d)
        cp.add_argument("--json", action="store_true", help="Machine-readable output", **d)
        cp.add_argument("-y", "--yes", action="store_true", help="Automatically confirm prompts", **d)
        return cp

    common = make_common(False)
    sub_common = make_common(True)

    p = argparse.ArgumentParser(
        prog="questtools",
        description="questtools – control Meta Quest & Android over ADB. "
                    "Debloat, telemetry, performance, apps, files – all reversible.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common],
    )
    p.add_argument("-V", "--version", action="version", version=f"Quest Tools {__version__} by hemiize")
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND", title="commands")
    for m in MODULES:
        m.register(sub, sub_common)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        if args.cmd:
            parser.parse_args([args.cmd, "--help"])
        if sys.stdin.isatty() and sys.stdout.isatty():
            from .tui import start  # no command: interactive interface
            return start(args.serial)
        parser.print_help()
        return 0
    ui.JSON_MODE = getattr(args, "json", False)
    ctx = Ctx(args)
    try:
        rc = args.func(ctx)
        return int(rc or 0)
    except AdbError as e:
        ui.err(e.message)
        if e.detail:
            for line in e.detail.splitlines()[:12]:
                print(ui.c("  " + line, "grey"), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print()
        ui.warn("Cancelled.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
