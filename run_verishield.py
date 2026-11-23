#!/usr/bin/env python3
"""Start VeriShield locally:  python run_verishield.py   ->  http://localhost:8000

Options are passed to ``python -m verishield serve`` (e.g. --port 9000, --no-demo, --host 0.0.0.0).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from verishield.__main__ import main  # noqa: E402

if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0].startswith("-"):
        args = ["serve", *args]
    sys.exit(main(args))
