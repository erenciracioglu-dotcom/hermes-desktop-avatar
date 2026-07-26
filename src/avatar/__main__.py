"""Entry point: python -m avatar"""

from __future__ import annotations

import sys
from pathlib import Path


def _ensure_src_on_path() -> None:
    # Allow `python -m avatar` when running from repo without install.
    root = Path(__file__).resolve().parents[2]
    src = root / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))


def main() -> int:
    _ensure_src_on_path()
    from avatar.app import run

    return run()


if __name__ == "__main__":
    raise SystemExit(main())
