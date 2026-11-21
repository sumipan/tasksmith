"""``python -m tasksmith`` entry point."""

from __future__ import annotations

import sys

from tasksmith.cli import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
