"""Developer console — tails the avatar log file with ANSI stripping.

Opened from the tray menu ("Hermes Console"). Shows ``avatar.log`` under
the user data directory (same file the Qt app writes via logging).
"""
from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QFileSystemWatcher, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import (
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

# Strip crude ANSI escape sequences so chat lines read cleanly.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mGKHF]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


class ConsoleWidget(QWidget):
    closed = Signal()

    def __init__(self, log_path: Optional[Path] = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Hermes Console")
        self.resize(820, 360)
        layout = QVBoxLayout(self)
        self.text = QPlainTextEdit(self)
        self.text.setReadOnly(True)
        self.text.setMaximumBlockCount(0)  # unlimited — do not clip log history
        font = QFont("Consolas")
        font.setStyleHint(QFont.Monospace)
        self.text.setFont(font)
        layout.addWidget(self.text)
        self.clear_btn = QPushButton("Clear", self)
        self.clear_btn.clicked.connect(self.text.clear)
        layout.addWidget(self.clear_btn)

        self._log_path = log_path
        self._last_size = 0
        self._watcher: QFileSystemWatcher | None = None
        if log_path:
            self._watcher = QFileSystemWatcher(self)
            self._watcher.fileChanged.connect(self._on_changed)
            # poll timer in case the file is replaced (truncated)
            self._timer = QTimer(self)
            self._timer.timeout.connect(self._poll)
            self._timer.start(750)

    def attach_log(self, log_path: Path) -> None:
        self._log_path = log_path
        if self._watcher is None:
            self._watcher = QFileSystemWatcher(self)
            self._watcher.fileChanged.connect(self._on_changed)
        if log_path.exists():
            self._watcher.addPath(str(log_path))
            self._last_size = 0
            self._poll()

    def _on_changed(self, _path: str) -> None:
        self._poll()

    def _poll(self) -> None:
        if not self._log_path or not self._log_path.exists():
            return
        try:
            data = self._log_path.read_bytes()
            if len(data) < self._last_size:
                # truncated/rotated — restart from the top
                self._last_size = 0
                self.text.clear()
            if len(data) > self._last_size:
                chunk = data[self._last_size :].decode("utf-8", errors="replace")
                self._last_size = len(data)
                self._append(chunk)
        except OSError:
            pass

    def _append(self, raw: str) -> None:
        cleaned = _strip_ansi(raw)
        self.text.moveCursor(QTextCursor.End)
        self.text.insertPlainText(cleaned)
        self.text.moveCursor(QTextCursor.End)

    def closeEvent(self, event):
        self.closed.emit()
        super().closeEvent(event)
