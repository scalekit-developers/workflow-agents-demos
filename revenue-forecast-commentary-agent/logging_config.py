"""
Structured logging with TTY-aware colors.

Supports:
  - TTY detection (auto-disable colors in CI/pipes)
  - LOG_LEVEL env var (DEBUG/INFO/WARNING/ERROR)
  - Redaction of secrets
  - Timestamp + level + message format
"""

import os
import sys
import logging
import re
import datetime


def _is_tty() -> bool:
    """Check if stdout is connected to a terminal."""
    return sys.stdout.isatty()


def _redact_secrets(text: str) -> str:
    """Replace common secrets with ***REDACTED***."""
    if not isinstance(text, str):
        return text
    patterns = [
        (r'sk[_-][\w-]{20,}', '***REDACTED***'),
        (r'skc_[\w-]+', '***REDACTED***'),
        (r'test_[\w-]{20,}', '***REDACTED***'),
        (r'Bearer\s+[\w-]+', 'Bearer ***REDACTED***'),
        (r'Authorization:\s+[\w-]+', 'Authorization: ***REDACTED***'),
        (r'"token":\s*"[^"]*"', '"token": "***REDACTED***"'),
        (r'"access_token":\s*"[^"]*"', '"access_token": "***REDACTED***"'),
        (r'"api_key":\s*"[^"]*"', '"api_key": "***REDACTED***"'),
    ]
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text


class ColorFormatter(logging.Formatter):
    """Colored formatter (only if TTY)."""

    COLORS = {
        'DEBUG': '\033[36m',
        'INFO': '\033[32m',
        'WARNING': '\033[33m',
        'ERROR': '\033[31m',
        'CRITICAL': '\033[35m',
    }
    RESET = '\033[0m'

    def __init__(self, use_color: bool = True):
        super().__init__()
        self.use_color = use_color and _is_tty()

    def format(self, record):
        timestamp = datetime.datetime.fromtimestamp(
            record.created, datetime.timezone.utc
        ).strftime('%H:%M:%S')

        levelname = record.levelname
        if self.use_color:
            color = self.COLORS.get(levelname, '')
            levelname = f"{color}{levelname}{self.RESET}"

        msg = record.getMessage()
        if record.exc_info:
            msg += '\n' + logging.Formatter().formatException(record.exc_info)
        msg = _redact_secrets(msg)

        return f"[{timestamp}] {levelname}: {msg}"


def setup_logging(name: str = __name__) -> logging.Logger:
    """
    Setup structured logging for the whole process.

    Configures the ROOT logger with the shared handler/formatter/level, so
    that every module's own `logging.getLogger(__name__)` call (connectors.py,
    aggregator.py, provisioning.py, state.py, ...) automatically inherits the
    same formatting and level via normal logger propagation, instead of only
    the single named logger returned here. Without this, per-module loggers
    default to the logging library's built-in WARNING level with no handler,
    and INFO-level messages (e.g. "connector -- ACTIVE" confirmations) would
    silently never print.

    Returns a logger configured with:
      - LOG_LEVEL env var (default INFO)
      - TTY-aware colors (auto-disabled in CI)
      - Timestamps in HH:MM:SS format
      - Secret redaction
    """
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, log_level, logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    handler.setFormatter(ColorFormatter(use_color=_is_tty()))
    root_logger.addHandler(handler)

    return logging.getLogger(name)
