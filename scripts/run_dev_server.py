"""Run api-core with a safe auto-reload scope for local development.

This avoids reload storms caused by watching the repository root, which may
include virtualenvs, SQLite files, and other generated content.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run api-core in reload mode")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=8001, help="Bind port")
    parser.add_argument(
        "--no-reload",
        action="store_true",
        help="Disable auto-reload",
    )
    args = parser.parse_args()

    reload_dirs = [
        str(PROJECT_ROOT / "src"),
        str(PROJECT_ROOT / "scripts"),
        str(PROJECT_ROOT / "propagate"),
    ]

    uvicorn.run(
        "src.main:app",
        host=args.host,
        port=args.port,
        reload=not args.no_reload,
        reload_dirs=reload_dirs,
    )


if __name__ == "__main__":
    main()
