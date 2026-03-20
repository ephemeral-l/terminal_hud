"""Core HUD engine — scroll region management, rendering, shell spawning."""

import atexit
import fcntl
import os
import pty
import signal
import struct
import sys
import termios
import threading
import time

from terminal_hud.colors import (
    BG_HUD, BOLD, DIM, FG_CYAN, FG_GRAY, FG_WHITE, RESET, V_LINE,
    bar, format_bytes_speed,
)
from terminal_hud.stats import StatsCollector

HUD_HEIGHT = 2  # lines reserved at bottom


def _get_terminal_size() -> tuple[int, int]:
    """Return (lines, cols) of the terminal."""
    sz = os.get_terminal_size()
    return sz.lines, sz.columns


class HUD:
    """Persistent terminal HUD with scroll region."""

    def __init__(
        self,
        interval: float = 1.0,
        color: bool = True,
        show_network: bool = True,
        interface: str | None = None,
    ):
        self.interval = interval
        self.color = color
        self.show_network = show_network
        self.collector = StatsCollector(interface=interface)
        self._running = False
        self._lock = threading.Lock()
        self._lines = 0
        self._cols = 0
        self._thread: threading.Thread | None = None
        self._child_pid: int = 0
        self._child_fd: int = -1  # pty fd for the child shell

    def start(self):
        """Start the HUD: set up scroll region, spawn shell, begin updates."""
        self._running = True
        self._lines, self._cols = _get_terminal_size()

        # Register cleanup
        atexit.register(self._cleanup)
        signal.signal(signal.SIGWINCH, self._on_resize)

        # Set up scroll region
        self._setup_scroll_region()

        # Start update thread
        self._thread = threading.Thread(target=self._update_loop, daemon=True)
        self._thread.start()

        # Spawn user's shell in the scroll region
        self._spawn_shell()

        # Shell exited — stop
        self.stop()

    def stop(self):
        """Stop the HUD and restore terminal."""
        if not self._running:
            return
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._cleanup()

    def _setup_scroll_region(self):
        """Reserve bottom lines for HUD using scroll region."""
        scroll_end = self._lines - HUD_HEIGHT
        if scroll_end < 1:
            scroll_end = 1
        # Set scroll region to top portion
        sys.stdout.write(f"\033[1;{scroll_end}r")
        # Move cursor to top-left of scroll region
        sys.stdout.write(f"\033[{scroll_end};1H")
        sys.stdout.flush()
        # Draw initial HUD
        self._render()

    def _cleanup(self):
        """Restore terminal state."""
        try:
            # Reset scroll region to full terminal
            sys.stdout.write("\033[r")
            # Clear HUD area
            lines, cols = _get_terminal_size()
            for i in range(HUD_HEIGHT):
                row = lines - HUD_HEIGHT + 1 + i
                sys.stdout.write(f"\033[{row};1H\033[2K")
            # Move cursor to a sane position
            sys.stdout.write(f"\033[{lines - HUD_HEIGHT};1H")
            # Show cursor
            sys.stdout.write("\033[?25h")
            sys.stdout.flush()
        except Exception:
            pass

    def _render(self):
        """Draw the HUD on the reserved bottom lines."""
        with self._lock:
            try:
                stats = self.collector.collect_all()
                lines, cols = self._lines, self._cols

                if lines < 4 or cols < 40:
                    return  # Terminal too small

                hud_row = lines - HUD_HEIGHT + 1

                # Save cursor position
                out = "\033[s"
                # Hide cursor during draw
                out += "\033[?25l"

                # --- Line 1: Stats ---
                out += f"\033[{hud_row};1H\033[2K"

                cpu_section = f" CPU {bar(stats.cpu_percent, 10)}"
                mem = stats.memory
                mem_section = (
                    f" MEM {bar(mem.percent, 10)}"
                    f" {FG_GRAY}{mem.used_gb:.1f}/{mem.total_gb:.1f}G{RESET}"
                )

                line1 = f"{BG_HUD}{FG_WHITE}{BOLD} {RESET}{BG_HUD}"
                line1 += cpu_section
                line1 += f" {FG_GRAY}{V_LINE}{RESET}{BG_HUD}"
                line1 += mem_section

                if self.show_network:
                    net = stats.network
                    net_section = (
                        f" NET"
                        f" {FG_CYAN}↓{RESET}{BG_HUD} {format_bytes_speed(net.down_bps)}"
                        f" {FG_CYAN}↑{RESET}{BG_HUD} {format_bytes_speed(net.up_bps)}"
                    )
                    line1 += f" {FG_GRAY}{V_LINE}{RESET}{BG_HUD}"
                    line1 += net_section

                # Erase to end of line (inherits BG color) instead of padding
                line1 += "\033[K"
                line1 += RESET
                out += line1

                # --- Line 2: Info bar ---
                out += f"\033[{hud_row + 1};1H\033[2K"
                info = (
                    f"{BG_HUD}{DIM}{FG_GRAY}"
                    f"  terminal-hud"
                    f" {V_LINE} interval: {self.interval}s"
                    f" {V_LINE} Ctrl+C to exit"
                    f"\033[K"
                )
                info += RESET
                out += info

                # Restore cursor position & show cursor
                out += "\033[u\033[?25h"

                sys.stdout.write(out)
                sys.stdout.flush()
            except Exception:
                pass  # Don't crash the update loop on render errors

    def _set_child_winsize(self):
        """Tell the child pty its usable size (full terminal minus HUD)."""
        if self._child_fd < 0:
            return
        child_rows = max(1, self._lines - HUD_HEIGHT)
        child_cols = self._cols
        try:
            winsize = struct.pack("HHHH", child_rows, child_cols, 0, 0)
            fcntl.ioctl(self._child_fd, termios.TIOCSWINSZ, winsize)
        except OSError:
            pass
        # Notify child process so it re-queries its terminal size
        if self._child_pid > 0:
            try:
                os.kill(self._child_pid, signal.SIGWINCH)
            except OSError:
                pass

    def _on_resize(self, signum, frame):
        """Handle terminal resize."""
        try:
            self._lines, self._cols = _get_terminal_size()
            scroll_end = self._lines - HUD_HEIGHT
            if scroll_end < 1:
                scroll_end = 1
            sys.stdout.write(f"\033[1;{scroll_end}r")
            sys.stdout.flush()
            # Propagate adjusted size to child (e.g. Claude Code)
            self._set_child_winsize()
            self._render()
        except Exception:
            pass

    def _update_loop(self):
        """Background thread: periodically refresh the HUD."""
        while self._running:
            self._render()
            time.sleep(self.interval)

    def _spawn_shell(self):
        """Spawn the user's shell as a child process."""
        shell = os.environ.get("SHELL", "/bin/bash")
        env = os.environ.copy()

        # Use pty.spawn for proper terminal handling
        # This gives the child shell full PTY access within the scroll region
        try:
            pid, fd = pty.fork()
        except OSError:
            # Fallback: just wait for Ctrl+C
            self._wait_forever()
            return

        if pid == 0:
            # Child process — exec the shell
            os.execvpe(shell, [shell], env)
        else:
            # Parent — store refs and set child pty to adjusted size
            self._child_pid = pid
            self._child_fd = fd
            self._set_child_winsize()
            self._relay_io(pid, fd)

    def _relay_io(self, pid: int, fd: int):
        """Relay I/O between the terminal and the child shell."""
        import select
        import tty

        old_settings = termios.tcgetattr(sys.stdin.fileno())
        try:
            tty.setraw(sys.stdin.fileno())

            while self._running:
                try:
                    rlist, _, _ = select.select(
                        [sys.stdin.fileno(), fd], [], [], 0.1
                    )
                except (ValueError, OSError):
                    break

                if sys.stdin.fileno() in rlist:
                    try:
                        data = os.read(sys.stdin.fileno(), 1024)
                        if not data:
                            break
                        os.write(fd, data)
                    except OSError:
                        break

                if fd in rlist:
                    try:
                        data = os.read(fd, 4096)
                        if not data:
                            break
                        os.write(sys.stdout.fileno(), data)
                    except OSError:
                        break

            # Wait for child to finish
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
        finally:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_settings)
            try:
                os.close(fd)
            except OSError:
                pass

    def _wait_forever(self):
        """Fallback: wait until interrupted."""
        try:
            while self._running:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
