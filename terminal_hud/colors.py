"""ANSI color utilities and threshold-based coloring."""

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"

# Foreground colors
FG_GREEN = "\033[38;5;82m"
FG_YELLOW = "\033[38;5;220m"
FG_RED = "\033[38;5;196m"
FG_CYAN = "\033[38;5;39m"
FG_WHITE = "\033[38;5;255m"
FG_GRAY = "\033[38;5;245m"

# Background for HUD bar
BG_HUD = "\033[48;5;236m"

# Box drawing
V_LINE = "│"
BLOCK_FULL = "█"
BLOCK_EMPTY = "░"


def color_by_threshold(pct: float) -> str:
    """Return ANSI color code based on usage percentage."""
    if pct < 60:
        return FG_GREEN
    elif pct < 85:
        return FG_YELLOW
    return FG_RED


def colorize(pct: float, text: str) -> str:
    """Colorize text based on percentage threshold."""
    return f"{color_by_threshold(pct)}{text}{RESET}"


def bar(pct: float, width: int = 10) -> str:
    """Render a colored bar like [████░░░░░░] 65%."""
    filled = int(pct / 100 * width)
    filled = max(0, min(width, filled))
    empty = width - filled
    color = color_by_threshold(pct)
    pct_str = f"{pct:4.0f}%"
    return (
        f"{FG_GRAY}[{RESET}"
        f"{color}{BLOCK_FULL * filled}{RESET}"
        f"{FG_GRAY}{BLOCK_EMPTY * empty}{RESET}"
        f"{FG_GRAY}]{RESET} "
        f"{color}{pct_str}{RESET}"
    )


def format_bytes_speed(bps: float) -> str:
    """Format bytes/sec to human-readable string."""
    if bps < 1024:
        return f"{bps:.0f}  B/s"
    elif bps < 1024 * 1024:
        return f"{bps / 1024:.1f} KB/s"
    elif bps < 1024 * 1024 * 1024:
        return f"{bps / (1024 * 1024):.1f} MB/s"
    else:
        return f"{bps / (1024 * 1024 * 1024):.1f} GB/s"
