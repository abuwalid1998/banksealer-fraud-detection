#!/usr/bin/env python3
"""Launch the BankSealer desktop application.

    python run_desktop.py                      # starts the enabled channels (mock by default)
    python run_desktop.py --no-autostart       # start channels manually from the toolbar
    python run_desktop.py --resume             # continue the previous session
"""
from banksealer.gui.main_window import main

if __name__ == "__main__":
    raise SystemExit(main())
