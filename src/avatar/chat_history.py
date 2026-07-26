"""Chat history widget — Qt model that backs the chat bubble list.

Persists each turn to a local JSON file so conversation survives avatar
restarts.  Storage path is resolved from Settings; default is
%APPDATA%/hermes-desktop-avatar/chat_history.json.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt, Signal

logger = logging.getLogger(__name__)


@dataclass
class ChatTurn:
    role: str  # "user" | "assistant"
    text: str
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())


class ChatHistoryModel(QAbstractListModel):
    """List model exposing ChatTurn rows to a QListView."""

    UserRole = Qt.UserRole + 1
    TextRole = Qt.UserRole + 2
    TimestampRole = Qt.UserRole + 3

    turn_added = Signal(int)  # row index

    def __init__(self, storage_path: Optional[Path] = None, parent=None) -> None:
        super().__init__(parent)
        self._turns: list[ChatTurn] = []
        self._storage_path = storage_path
        self._lock = threading.Lock()
        self._load()

    # --- Qt model API ---
    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._turns)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self._turns):
            return None
        turn = self._turns[index.row()]
        if role == self.UserRole:
            return turn.role
        if role == self.TextRole:
            return turn.text
        if role == self.TimestampRole:
            return turn.timestamp
        if role == Qt.DisplayRole:
            return f"{turn.role}: {turn.text}"
        return None

    def roleNames(self):
        return {
            self.UserRole: b"role",
            self.TextRole: b"text",
            self.TimestampRole: b"timestamp",
        }

    # --- public API ---
    def add_turn(self, role: str, text: str) -> int:
        with self._lock:
            turn = ChatTurn(role=role, text=text)
            self.beginInsertRows(QModelIndex(), len(self._turns), len(self._turns))
            self._turns.append(turn)
            self.endInsertRows()
            self._save()
            idx = len(self._turns) - 1
            self.turn_added.emit(idx)
            return idx

    def clear(self) -> None:
        with self._lock:
            self.beginResetModel()
            self._turns.clear()
            self.endResetModel()
            self._save()

    def turns(self) -> list[ChatTurn]:
        return list(self._turns)

    # --- persistence ---
    def _load(self) -> None:
        if not self._storage_path or not self._storage_path.exists():
            return
        try:
            raw = json.loads(self._storage_path.read_text(encoding="utf-8"))
            self.beginResetModel()
            self._turns = [ChatTurn(**t) for t in raw]
            self.endResetModel()
        except Exception:
            logger.exception("chat history load failed; starting empty")

    def _save(self) -> None:
        if not self._storage_path:
            return
        try:
            self._storage_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._storage_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps([asdict(t) for t in self._turns], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self._storage_path)
        except Exception:
            logger.exception("chat history save failed")
