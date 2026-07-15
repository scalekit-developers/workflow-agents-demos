"""Structured logging with colors and icons."""
import sys
import logging
from typing import ClassVar


class ColorFormatter(logging.Formatter):
    """Colorized formatter with status icons."""

    COLORS: ClassVar[dict] = {
        logging.DEBUG: "\033[36m",    # cyan
        logging.INFO: "\033[32m",     # green
        logging.WARNING: "\033[33m",  # yellow
        logging.ERROR: "\033[31m",    # red
    }
    RESET: ClassVar[str] = "\033[0m"

    ICONS: ClassVar[dict] = {
        logging.DEBUG: "▶",
        logging.INFO: "✔",
        logging.WARNING: "⚠",
        logging.ERROR: "✖",
    }

    def format(self, record: logging.LogRecord) -> str:
        is_tty = sys.stdout.isatty()
        if not is_tty:
            return f"[{self.formatTime(record, '%H:%M:%S')}] {record.levelname[0]}: {record.getMessage()}"

        color = self.COLORS.get(record.levelno, self.RESET)
        icon = self.ICONS.get(record.levelno, " ")
        timestamp = self.formatTime(record, "%H:%M:%S")
        level_name = record.levelname[0]
        return (
            f"{color}{icon} [{timestamp}] {level_name}: {record.getMessage()}{self.RESET}"
        )


def setup_logging(level: str = "INFO") -> logging.Logger:
    """Configure structured logging with colors."""
    logger = logging.getLogger("offer-letter-agent")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    if logger.handlers:
        return logger

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(ColorFormatter())
    logger.addHandler(handler)

    return logger
