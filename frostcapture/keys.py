"""Single keypresses without Enter, on Windows and POSIX terminals."""

import os
import sys


class KeyReader:
    """Context manager; poll() returns the keys pressed since the last call. Esc comes back as "\\x1b"."""

    def __init__(self):
        self.enabled = sys.stdin is not None and sys.stdin.isatty()
        self._saved = None

    def __enter__(self):
        if self.enabled and os.name != "nt":
            import termios
            import tty
            fd = sys.stdin.fileno()
            self._saved = termios.tcgetattr(fd)
            tty.setcbreak(fd)
        return self

    def __exit__(self, *exc):
        if self._saved is not None:
            import termios
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._saved)
            self._saved = None

    def poll(self) -> list:
        if not self.enabled:
            return []
        keys = []
        if os.name == "nt":
            import msvcrt
            while msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\x00", "\xe0"):  # arrow and function keys: two characters, ignored
                    msvcrt.getwch()
                    continue
                keys.append(ch)
        else:
            import select
            while select.select([sys.stdin], [], [], 0)[0]:
                ch = os.read(sys.stdin.fileno(), 1).decode(errors="ignore")
                if not ch:
                    break
                keys.append(ch)
        return keys
