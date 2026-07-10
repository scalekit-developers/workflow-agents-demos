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

def _is_tty() -> bool:
    """Check if stdout is connected to a terminal."""
    return sys.stdout.isatty()

def _redact_secrets(text: str) -> str:
    """Replace common secrets with ***REDACTED***."""
    if not isinstance(text, str):
        return text
    patterns = [
        (r'sk[_-][\w-]{20,}', '***REDACTED***'),  # Scalekit keys
        (r'sk_[\w-]+', '***REDACTED***'),          # SDK tokens
        (r'Bearer\s+[\w-]+', 'Bearer ***REDACTED***'),
        (r'Authorization:\s+[\w-]+', 'Authorization: ***REDACTED***'),
        (r'"token":\s*"[^"]*"', '"token": "***REDACTED***"'),
        (r'"access_token":\s*"[^"]*"', '"access_token": "***REDACTED***"'),
    ]
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text

class ColorFormatter(logging.Formatter):
    """Colored formatter (only if TTY)."""

    COLORS = {
        'DEBUG': '\033[36m',    # Cyan
        'INFO': '\033[32m',     # Green
        'WARNING': '\033[33m',  # Yellow
        'ERROR': '\033[31m',    # Red
        'CRITICAL': '\033[35m', # Magenta
    }
    RESET = '\033[0m'

    def __init__(self, use_color: bool = True):
        super().__init__()
        self.use_color = use_color and _is_tty()

    def format(self, record):
        if self.use_color:
            levelname = record.levelname
            color = self.COLORS.get(levelname, '')
            record.levelname = f"{color}{levelname}{self.RESET}"

        msg = super().format(record)
        msg = _redact_secrets(msg)
        return msg

def setup_logging(name: str = __name__) -> logging.Logger:
    """
    Setup structured logging.

    Returns a logger configured with:
      - LOG_LEVEL env var (default INFO)
      - TTY-aware colors (auto-disabled in CI)
      - Timestamps in HH:MM:SS format
      - Secret redaction
    """
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    try:
        level = getattr(logging, log_level)
    except AttributeError:
        level = logging.INFO

    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)

    formatter = ColorFormatter(use_color=_is_tty())
    formatter.format = lambda record: _format_record(record, formatter)
    handler.setFormatter(formatter)

    logger.addHandler(handler)
    logger.propagate = False

    return logger

def _format_record(record: logging.LogRecord, formatter: ColorFormatter) -> str:
    """Custom format: [HH:MM:SS] LEVEL: message"""
    timestamp = record.created
    dt = __import__('datetime').datetime.fromtimestamp(
        timestamp, __import__('datetime').timezone.utc
    ).strftime('%H:%M:%S')

    levelname = record.levelname
    if formatter.use_color:
        color = formatter.COLORS.get(levelname, '')
        levelname = f"{color}{levelname}{formatter.RESET}"

    msg = record.getMessage()
    if record.exc_info:
        msg += '\n' + logging.Formatter().formatException(record.exc_info)

    msg = _redact_secrets(msg)
    return f"[{dt}] {levelname}: {msg}"
