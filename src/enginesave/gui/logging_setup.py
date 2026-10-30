"""GUI logging: ``%APPDATA%\\EngineSave\\logs\\gui.log`` plus the window's log panel (D23).

Both handlers hang off the ``enginesave`` logger, so library warnings (for example a damaged
object in the store) reach the panel as well. The panel handler only queues text; the window
drains it on the Tk thread, which keeps worker threads away from widgets.
"""

from __future__ import annotations

import logging
import queue
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_NAME = "gui.log"
MAX_BYTES = 1024 * 1024
BACKUP_COUNT = 4  # gui.log + gui.log.1..4: at most five files of 1 MB
FILE_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
PANEL_DATE_FORMAT = "%H:%M:%S"
LOGGER_NAME = "enginesave"


class PanelFormatter(logging.Formatter):
    """``HH:MM:SS  message``; warnings and errors carry their level, tracebacks stay in the file."""

    def format(self, record: logging.LogRecord) -> str:
        level = f"[{record.levelname}] " if record.levelno >= logging.WARNING else ""
        return f"{self.formatTime(record, PANEL_DATE_FORMAT)}  {level}{record.getMessage()}"


class PanelHandler(logging.Handler):
    """Formats records for the log panel and queues them (safe to call from any thread)."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.lines: queue.SimpleQueue[str] = queue.SimpleQueue()
        self.setFormatter(PanelFormatter())

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.put(self.format(record))
        except Exception:  # noqa: BLE001 - logging must never raise into the caller
            self.handleError(record)

    def drain(self) -> list[str]:
        lines = []
        while True:
            try:
                lines.append(self.lines.get_nowait())
            except queue.Empty:
                return lines


def setup_logging(directory: Path) -> tuple[PanelHandler, RotatingFileHandler]:
    """Attach the rotating file handler and a panel handler to the ``enginesave`` logger."""
    directory.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        directory / LOG_NAME, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter(FILE_FORMAT))
    panel = PanelHandler()
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.addHandler(file_handler)
    logger.addHandler(panel)
    return panel, file_handler


def close_logging(*handlers: logging.Handler) -> None:
    logger = logging.getLogger(LOGGER_NAME)
    for handler in handlers:
        logger.removeHandler(handler)
        handler.close()
