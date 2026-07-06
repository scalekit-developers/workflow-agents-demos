"""Structured logging with colors and icons."""
import sys
import logging


class ColorFormatter(logging.Formatter):
    """Colorized formatter with status icons."""

    COLORS = {
        logging.DEBUG: "\033[36m",    # cyan
        logging.INFO: "\033[32m",     # green
        logging.WARNING: "\033[33m",  # yellow
        logging.ERROR: "\033[31m",    # red
    }
    RESET = "\033[0m"

    ICONS = {
        logging.DEBUG: "▶",
        logging.INFO: "✔",
        logging.WARNING: "⚠",
        logging.ERROR: "✖",
    }

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelno, self.RESET)
        icon = self.ICONS.get(record.levelno, " ")
        timestamp = self.formatTime(record, "%H:%M:%S")
        level_name = record.levelname[0]
        return (
            f"{color}{icon} [{timestamp}] {level_name}: {record.getMessage()}{self.RESET}"
        )


def setup_logging(level: str = "INFO") -> logging.Logger:
    """Configure structured logging with colors."""
    logger = logging.getLogger("granola-hubspot")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    if logger.handlers:
        return logger

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(ColorFormatter())
    logger.addHandler(handler)

    return logger
