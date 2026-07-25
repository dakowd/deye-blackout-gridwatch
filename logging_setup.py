"""
Shared logging setup for this project.

Writes to both:
  - logs/deyeblackoutgridwatch.log -- rotates at midnight, keeps 7 days of history
    (logs/deyeblackoutgridwatch.log.2026-07-24, .2026-07-23, etc. -- older ones are
    deleted automatically)
  - console (stdout) -- so you still see live output when running in the
    foreground, tmux, or via `journalctl -u deyeblackoutgridwatch -f` under systemd

The file gets everything, including [DEBUG] diagnostics from client.py.
The console only shows INFO and above, to keep live output readable --
the full detail is still there in the log files if you need to dig in.
"""

import logging
import os
from logging.handlers import TimedRotatingFileHandler

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "deyeblackoutgridwatch.log")

_FORMATTER = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")


def get_logger(name: str = "deyeblackoutgridwatch") -> logging.Logger:
    logger = logging.getLogger(name)

    # Guard against adding duplicate handlers if this is called more than
    # once (e.g. imported from both controller.py and client.py)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    file_handler = TimedRotatingFileHandler(
        LOG_FILE, when="midnight", interval=1, backupCount=7, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(_FORMATTER)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(_FORMATTER)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    logger.propagate = False

    return logger
