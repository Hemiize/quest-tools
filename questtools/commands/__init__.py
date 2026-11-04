"""Command modules. Each module provides register(sub, common)."""

from __future__ import annotations

import argparse


def add(sub, common, name: str, help: str, func=None, aliases=(), description: str | None = None):
    p = sub.add_parser(name, help=help, aliases=list(aliases), parents=[common],
                       description=description or help, formatter_class=argparse.RawDescriptionHelpFormatter)
    if func:
        p.set_defaults(func=func)
    return p


def group(sub, common, name: str, help: str, aliases=()):
    """Command group with its own subcommands; without a subcommand → help."""
    p = add(sub, common, name, help, aliases=aliases)
    p.set_defaults(func=lambda ctx, _p=p: _p.print_help())
    return p, p.add_subparsers(metavar="ACTION", title="actions")


def dry(p):
    p.add_argument("-n", "--dry-run", action="store_true", help="Only show, change nothing")
    return p


def onoff(p, name="state"):
    p.add_argument(name, choices=["on", "off"], help="on or off")
    return p


def is_on(v: str) -> bool:
    return v == "on"
