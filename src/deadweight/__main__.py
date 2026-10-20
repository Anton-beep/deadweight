"""``python -m deadweight``: the same entry point the console script installs."""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
