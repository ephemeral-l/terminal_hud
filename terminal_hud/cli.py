"""CLI entry point for terminal-hud."""

import argparse
import signal
import sys

from terminal_hud.hud import HUD


def main():
    parser = argparse.ArgumentParser(
        prog="terminal-hud",
        description="Persistent terminal HUD showing CPU, memory, and network usage.",
    )
    parser.add_argument(
        "-i", "--interval",
        type=float,
        default=1.0,
        help="Refresh interval in seconds (default: 1.0, min: 0.2)",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable colored output",
    )
    parser.add_argument(
        "--no-network",
        action="store_true",
        help="Hide network stats",
    )
    parser.add_argument(
        "--interface",
        type=str,
        default=None,
        help="Network interface to monitor (default: all)",
    )

    args = parser.parse_args()

    # Enforce minimum interval
    interval = max(0.2, args.interval)

    hud = HUD(
        interval=interval,
        color=not args.no_color,
        show_network=not args.no_network,
        interface=args.interface,
    )

    # Handle Ctrl+C gracefully
    def sigint_handler(sig, frame):
        hud.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, sigint_handler)

    try:
        hud.start()
    except Exception as e:
        hud.stop()
        print(f"terminal-hud error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
