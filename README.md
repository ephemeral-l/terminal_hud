# terminal-hud

A lightweight, persistent terminal status bar that displays real-time CPU, memory, and network usage — always visible at the bottom of your terminal while you work.

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ $ your normal shell here...                                                  │
│ $ ls -la                                                                     │
│ $ git status                                                                 │
│                                                                              │
├──────────────────────────────────────────────────────────────────────────────┤
│ CPU [████░░░░░░]  35% │ MEM [██████░░░░]  62% 5.0/8.0G │ NET ↓ 1.2 MB/s ↑… │
│  terminal-hud │ interval: 1.0s │ Ctrl+C to exit                             │
└──────────────────────────────────────────────────────────────────────────────┘
```

- **Off by default** — you start it when you want it, `Ctrl+C` or `exit` to stop
- **Coexists with Claude Code** — properly propagates terminal size so TUI apps render correctly above the HUD
- **Negligible overhead** — 0.02% CPU at 1s interval, 15 MB RAM, zero memory leak

## Installation

### 1. Clone the repo

```bash
git clone <your-repo-url>
cd terminal_hud
```

### 2. Create the conda environment

```bash
conda create -n terminal_hud python=3.11 -y
conda activate terminal_hud
```

### 3. Install

```bash
pip install -e .
```

This installs the `terminal-hud` command into your conda environment.

## Usage

Activate the environment and run:

```bash
conda activate terminal_hud
terminal-hud
```

This opens a new shell session with the HUD pinned at the bottom. Everything you do in the shell (run commands, launch `claude`, use `vim`, etc.) works normally above the HUD.

To stop, type `exit` or press `Ctrl+C`.

### Options

```
terminal-hud [OPTIONS]

  -i, --interval SECONDS   Refresh interval (default: 1.0, min: 0.2)
  --no-color               Disable color output
  --no-network             Hide network stats
  --interface NAME         Monitor a specific network interface (default: all)
  -h, --help               Show help
```

### Examples

```bash
# Default: 1 second refresh
terminal-hud

# Faster refresh (half second)
terminal-hud -i 0.5

# CPU and memory only, no network
terminal-hud --no-network

# Slow refresh for low-power/SSH sessions
terminal-hud -i 5
```

## Using with Claude Code

Start `terminal-hud`, then launch `claude` inside it:

```bash
terminal-hud
# Now inside the HUD shell:
claude
```

Claude Code renders its UI (input box, output, status bar) in the space above the HUD. The HUD stays pinned at the very bottom. Terminal resizes (including moving between monitors with different resolutions) are handled correctly — resize signals are debounced and propagated to Claude Code without display corruption.

## How it works

1. **Scroll region** — reserves the bottom 2 terminal lines using ANSI escape sequence `\033[1;Nr` so the shell above scrolls independently
2. **PTY fork** — spawns your `$SHELL` in a child PTY with the adjusted window size (`lines - 2`)
3. **I/O relay** — a `select()` loop relays keystrokes and output between you and the child shell
4. **Background thread** — collects CPU/MEM/NET stats via `psutil` every interval and redraws the HUD
5. **Resize handling** — SIGWINCH is debounced (150ms), then the scroll region + child PTY size are updated atomically under an I/O lock to prevent display corruption

## Project structure

```
terminal_hud/
├── environment.yml          # Conda environment definition
├── pyproject.toml           # Package metadata + entry point
├── README.md
└── terminal_hud/
    ├── __init__.py
    ├── __main__.py          # python -m terminal_hud
    ├── cli.py               # Argument parsing, entry point
    ├── hud.py               # Core engine: scroll region, PTY, I/O relay, rendering
    ├── stats.py             # System stats collection (psutil)
    └── colors.py            # ANSI color helpers, bar rendering
```

## Requirements

- Python >= 3.10
- Linux or macOS (uses PTY, `/proc` filesystem for stats)
- psutil >= 5.9.0
