"""Structured logging with colors and indicators."""
import sys
import logging
from typing import ClassVar


class ColorFormatter(logging.Formatter):
    """Colorized formatter with status indicators."""

    COLORS: ClassVar[dict] = {
        logging.DEBUG: "\033[36m",
        logging.INFO: "\033[32m",
        logging.WARNING: "\033[33m",
        logging.ERROR: "\033[31m",
    }
    RESET: ClassVar[str] = "\033[0m"

    ICONS: ClassVar[dict] = {
        logging.DEBUG: "^",
        logging.INFO: "+",
        logging.WARNING: "!",
        logging.ERROR: "X",
    }

    def format(self, record: logging.LogRecord) -> str:
        is_tty = sys.stdout.isatty()
        if not is_tty:
            msg = f"[{self.formatTime(record, '%H:%M:%S')}] {record.levelname[0]}: {record.getMessage()}"
            if record.exc_info:
                msg += "\n" + self.formatException(record.exc_info)
            return msg

        color = self.COLORS.get(record.levelno, self.RESET)
        icon = self.ICONS.get(record.levelno, " ")
        timestamp = self.formatTime(record, "%H:%M:%S")
        level_name = record.levelname[0]
        msg = f"{color}{icon} [{timestamp}] {level_name}: {record.getMessage()}{self.RESET}"
        if record.exc_info:
            msg += "\n" + self.formatException(record.exc_info)
        return msg


def setup_logging(level: str = "INFO") -> logging.Logger:
    """Configure structured logging with colors."""
    logger = logging.getLogger("gong-attio-slack")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    if logger.handlers:
        return logger

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(ColorFormatter())
    logger.addHandler(handler)

    return logger
