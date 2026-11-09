"""Command-line entry point with a tiny subcommand registry.

Later modules add subcommands by calling :func:`register` at import time::

    def setup(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--plan-json")
        parser.set_defaults(handler=run)      # run(args: argparse.Namespace) -> int

    cli.register("changeset", setup)

``setup`` receives the subparser created for ``name`` and must set a ``handler``
default; ``main`` calls ``handler(args)`` and returns its exit code.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence

from stagegate import __version__

SetupFn = Callable[[argparse.ArgumentParser], None]

_COMMANDS: dict[str, SetupFn] = {}


def register(name: str, setup: SetupFn) -> None:
    """Register subcommand ``name``; ``setup`` configures its subparser."""
    if name in _COMMANDS:
        raise ValueError(f"subcommand already registered: {name}")
    _COMMANDS[name] = setup


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stagegate", description=__doc__.split("\n")[0])
    parser.add_argument("--version", action="store_true", help="print version and exit")
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    for name, setup in _COMMANDS.items():
        setup(sub.add_parser(name))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.version:
        print(f"stagegate {__version__}")
        return 0
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help(sys.stderr)
        return 2
    return handler(args)


def entrypoint() -> None:
    import stagegate.approvals  # noqa: F401  (registers its subcommand)
    import stagegate.binding  # noqa: F401  (registers its subcommand)
    import stagegate.cascade  # noqa: F401  (registers its subcommand)
    import stagegate.changed  # noqa: F401  (registers its subcommand)
    import stagegate.changeset  # noqa: F401  (registers its subcommand)
    import stagegate.cockpit.collect  # noqa: F401  (registers its subcommand)
    import stagegate.config  # noqa: F401  (registers its subcommand)
    import stagegate.drift  # noqa: F401  (registers its subcommand)
    import stagegate.features  # noqa: F401  (registers its subcommand)
    import stagegate.findings  # noqa: F401  (registers its subcommand)
    import stagegate.guard  # noqa: F401  (registers its subcommand)
    import stagegate.issues  # noqa: F401  (registers its subcommand)
    import stagegate.ledger  # noqa: F401  (registers its subcommand)
    import stagegate.maintip  # noqa: F401  (registers its subcommand)
    import stagegate.notify  # noqa: F401  (registers its subcommand)
    import stagegate.prcomment  # noqa: F401  (registers its subcommand)
    import stagegate.sarif  # noqa: F401  (registers its subcommand)

    raise SystemExit(main())
