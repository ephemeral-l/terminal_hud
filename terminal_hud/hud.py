"""Core HUD engine — scroll region management, rendering, shell spawning."""

import atexit
import fcntl
import os
import pty
import select
import signal
import struct
import sys
import termios
import threading
import time
import tty

from terminal_hud.colors import (
    BG_HUD, BOLD, DIM, FG_CYAN, FG_GRAY, FG_WHITE, RESET, V_LINE,
    bar, format_bytes_speed,
)
from terminal_hud.stats import StatsCollector

HUD_HEIGHT = 2  # lines reserved at bottom
RESIZE_DEBOUNCE_S = 0.15  # wait 150ms for resize signals to settle


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
        # Single lock serializes ALL writes to stdout (HUD render + child relay)
        self._io_lock = threading.Lock()
        self._lines = 0
        self._cols = 0
        self._thread: threading.Thread | None = None
        self._child_pid: int = 0
        self._child_fd: int = -1

        # Resize state — signal handler sets flag, loops process it
        self._resize_pending = False
        self._resize_time: float = 0.0

        # Alternate screen tracking — full-screen apps (tmux, vim, less)
        # enter alternate screen mode which resets scroll regions.
        # We pause HUD rendering and re-apply scroll region on exit.
        self._alt_screen = False
        self._need_scroll_reset = False

    def start(self):
        """Start the HUD: set up scroll region, spawn shell, begin updates."""
        self._running = True
        self._lines, self._cols = _get_terminal_size()

        atexit.register(self._cleanup)
        signal.signal(signal.SIGWINCH, self._on_resize)

        self._setup_scroll_region()

        self._thread = threading.Thread(target=self._update_loop, daemon=True)
        self._thread.start()

        self._spawn_shell()
        self.stop()

    def stop(self):
        """Stop the HUD and restore terminal."""
        if not self._running:
            return
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._cleanup()

    # --- Terminal setup / teardown ---

    def _setup_scroll_region(self):
        """Reserve bottom lines for HUD using scroll region."""
        scroll_end = max(1, self._lines - HUD_HEIGHT)
        with self._io_lock:
            sys.stdout.write(f"\033[1;{scroll_end}r")
            sys.stdout.write(f"\033[{scroll_end};1H")
            sys.stdout.flush()
        self._render()

    def _cleanup(self):
        """Restore terminal state."""
        try:
            with self._io_lock:
                sys.stdout.write("\033[r")
                lines, cols = _get_terminal_size()
                for i in range(HUD_HEIGHT):
                    row = lines - HUD_HEIGHT + 1 + i
                    sys.stdout.write(f"\033[{row};1H\033[2K")
                sys.stdout.write(f"\033[{lines - HUD_HEIGHT};1H")
                sys.stdout.write("\033[?25h")
                sys.stdout.flush()
        except Exception:
            pass

    # --- Rendering ---

    def _build_hud_str(self, lines: int, cols: int) -> str:
        """Build the HUD output string. Pure function, no I/O."""
        stats = self.collector.collect_all()

        if lines < 4 or cols < 40:
            return ""

        hud_row = lines - HUD_HEIGHT + 1

        parts = [
            "\033[s",       # save cursor
            "\033[?25l",    # hide cursor
            f"\033[{hud_row};1H\033[2K",  # move to HUD line 1, clear
        ]

        # Line 1: stats
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
            line1 += (
                f" {FG_GRAY}{V_LINE}{RESET}{BG_HUD}"
                f" NET"
                f" {FG_CYAN}\u2193{RESET}{BG_HUD} {format_bytes_speed(net.down_bps)}"
                f" {FG_CYAN}\u2191{RESET}{BG_HUD} {format_bytes_speed(net.up_bps)}"
            )

        line1 += "\033[K" + RESET
        parts.append(line1)

        # Line 2: info
        parts.append(f"\033[{hud_row + 1};1H\033[2K")
        parts.append(
            f"{BG_HUD}{DIM}{FG_GRAY}"
            f"  terminal-hud"
            f" {V_LINE} interval: {self.interval}s"
            f" {V_LINE} Ctrl+C to exit"
            f"\033[K{RESET}"
        )

        # Restore cursor + show cursor
        parts.append("\033[u\033[?25h")

        return "".join(parts)

    def _render(self):
        """Build HUD string then write it atomically under the I/O lock."""
        try:
            out = self._build_hud_str(self._lines, self._cols)
            if not out:
                return
            with self._io_lock:
                sys.stdout.write(out)
                sys.stdout.flush()
        except Exception:
            pass

    # --- Resize handling ---

    def _on_resize(self, signum, frame):
        """SIGWINCH handler — minimal work. Just record the event."""
        self._resize_pending = True
        self._resize_time = time.monotonic()

    def _process_resize(self):
        """Apply a pending resize after debounce period has elapsed.

        Called from the relay loop (main thread) so we can coordinate
        with child I/O and avoid interleaving.
        """
        self._resize_pending = False
        try:
            self._lines, self._cols = _get_terminal_size()
        except OSError:
            return

        # Tell child shell the new usable size
        self._set_child_winsize()

        if self._alt_screen:
            # Full-screen app owns the display — don't touch scroll region.
            # Scroll region will be re-applied when alt screen exits.
            return

        scroll_end = max(1, self._lines - HUD_HEIGHT)

        # Update scroll region atomically
        with self._io_lock:
            sys.stdout.write(f"\033[1;{scroll_end}r")
            sys.stdout.flush()

        # Re-render HUD (acquires _io_lock internally)
        self._render()

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
        if self._child_pid > 0:
            try:
                os.kill(self._child_pid, signal.SIGWINCH)
            except OSError:
                pass

    # --- Alternate screen detection ---

    # Sequences that full-screen apps (tmux, vim, less) use.
    # Enter: save main screen, switch to alt buffer.
    # Exit:  switch back to main screen, restore.
    _ALT_ENTER = (b"\033[?1049h", b"\033[?47h", b"\033[?1047h")
    _ALT_EXIT = (b"\033[?1049l", b"\033[?47l", b"\033[?1047l")

    def _scan_alt_screen(self, data: bytes):
        """Check child output for alternate screen enter/exit sequences."""
        for seq in self._ALT_ENTER:
            if seq in data:
                self._alt_screen = True
                return
        for seq in self._ALT_EXIT:
            if seq in data:
                self._alt_screen = False
                self._need_scroll_reset = True
                return

    def _restore_scroll_region(self):
        """Re-establish scroll region after a full-screen app exits."""
        self._need_scroll_reset = False
        try:
            self._lines, self._cols = _get_terminal_size()
        except OSError:
            return
        scroll_end = max(1, self._lines - HUD_HEIGHT)
        with self._io_lock:
            sys.stdout.write(f"\033[1;{scroll_end}r")
            # Move cursor into the scroll region (not in HUD area)
            sys.stdout.write(f"\033[{scroll_end};1H")
            sys.stdout.flush()

    # --- Update loop (background thread) ---

    def _update_loop(self):
        """Background thread: periodically refresh the HUD."""
        while self._running:
            if self._need_scroll_reset:
                self._restore_scroll_region()

            if self._alt_screen:
                # Full-screen app active — don't render HUD
                time.sleep(0.1)
            elif self._resize_pending:
                # Resize settling — poll quickly
                time.sleep(0.05)
            else:
                self._render()
                time.sleep(self.interval)

    # --- Shell spawning + I/O relay ---

    def _spawn_shell(self):
        """Spawn the user's shell as a child process."""
        shell = os.environ.get("SHELL", "/bin/bash")
        env = os.environ.copy()

        try:
            pid, fd = pty.fork()
        except OSError:
            self._wait_forever()
            return

        if pid == 0:
            os.execvpe(shell, [shell], env)
        else:
            self._child_pid = pid
            self._child_fd = fd
            self._set_child_winsize()
            self._relay_io(pid, fd)

    def _relay_io(self, pid: int, fd: int):
        """Relay I/O between the terminal and the child shell.

        This is the main thread's hot loop. It also handles debounced
        resize processing between I/O cycles.
        """
        stdin_fd = sys.stdin.fileno()
        stdout_fd = sys.stdout.fileno()

        old_settings = termios.tcgetattr(stdin_fd)
        try:
            tty.setraw(stdin_fd)

            while self._running:
                # Process resize if debounce period has elapsed
                if self._resize_pending:
                    elapsed = time.monotonic() - self._resize_time
                    if elapsed >= RESIZE_DEBOUNCE_S:
                        self._process_resize()

                try:
                    rlist, _, _ = select.select(
                        [stdin_fd, fd], [], [], 0.1
                    )
                except (ValueError, OSError):
                    break

                if stdin_fd in rlist:
                    try:
                        data = os.read(stdin_fd, 1024)
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
                        # Detect alternate screen mode (tmux, vim, less)
                        self._scan_alt_screen(data)
                        # Write child output under I/O lock so it never
                        # interleaves with HUD render escape sequences
                        with self._io_lock:
                            os.write(stdout_fd, data)
                    except OSError:
                        break

            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
        finally:
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_settings)
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
