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


_REDACTION_PATTERNS = [
    (re.compile(r'sk[_-][\w-]{20,}', re.IGNORECASE), '***REDACTED***'),
    (re.compile(r'skc_[\w-]+', re.IGNORECASE), '***REDACTED***'),
    (re.compile(r'test_[\w-]{20,}', re.IGNORECASE), '***REDACTED***'),
    (re.compile(r'Bearer\s+[\w-]+', re.IGNORECASE), 'Bearer ***REDACTED***'),
    (re.compile(r'Authorization:\s+[\w-]+', re.IGNORECASE), 'Authorization: ***REDACTED***'),
    (re.compile(r'"token":\s*"[^"]*"', re.IGNORECASE), '"token": "***REDACTED***"'),
    (re.compile(r'"access_token":\s*"[^"]*"', re.IGNORECASE), '"access_token": "***REDACTED***"'),
    (re.compile(r'"api_key":\s*"[^"]*"', re.IGNORECASE), '"api_key": "***REDACTED***"'),
]


_exact_secrets = []  # populated by register_secret(); redacted in addition to the pattern-based rules below


def register_secret(value: str) -> None:
    """
    Register an exact secret value (e.g. SCALEKIT_CLIENT_SECRET) for
    redaction, in addition to the pattern-based rules below. Pattern
    matching alone only catches secrets with a recognizable shape (sk-,
    Bearer, test_, ...); a secret with no such prefix would otherwise be
    logged in full if it ever appeared in an SDK exception message.
    """
    if value and value.strip():
        _exact_secrets.append(value.strip())


def _redact_secrets(text: str) -> str:
    """Replace common secrets with ***REDACTED***."""
    if not isinstance(text, str):
        return text
    for secret in _exact_secrets:
        text = text.replace(secret, '***REDACTED***')
    for pattern, replacement in _REDACTION_PATTERNS:
        text = pattern.sub(replacement, text)
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
        ).strftime('%H:%M:%SZ')

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
    provisioning.py, state.py, ...) automatically inherits the same
    formatting and level via normal logger propagation, instead of only the
    single named logger returned here. Without this, per-module loggers
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
    valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    level = getattr(logging, log_level) if log_level in valid_levels else logging.INFO

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    handler.setFormatter(ColorFormatter(use_color=_is_tty()))
    root_logger.addHandler(handler)

    return logging.getLogger(name)
