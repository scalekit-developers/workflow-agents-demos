"""
Structured logging with TTY-aware colors and secret redaction.

Features:
- Automatic TTY detection (colors auto-disable in CI/pipes)
- Structured log levels (DEBUG, INFO, WARNING, ERROR, CRITICAL)
- Secret redaction for API keys, tokens, and credentials
- Timestamp + level + message format
- Emoji indicators for quick status scanning
"""

import os
import sys
import logging
import re
from datetime import datetime, timezone


def _is_tty() -> bool:
    """Check if stdout is connected to a terminal."""
    return sys.stdout.isatty()


def _redact_secrets(text: str) -> str:
    """Replace common secrets with ***REDACTED***."""
    if not isinstance(text, str):
        return text
    patterns = [
        (r'sk[_-][\w-]{20,}', '***REDACTED***'),              # Scalekit keys
        (r'xoxb-[\w-]+', '***REDACTED***'),                   # Slack bot token
        (r'xoxp-[\w-]+', '***REDACTED***'),                   # Slack user token
        (r'Bearer\s+[\w-]+', 'Bearer ***REDACTED***'),        # Bearer tokens
        (r'Authorization:\s+[\w-]+', 'Authorization: ***REDACTED***'),
        (r'"token":\s*"[^"]*"', '"token": "***REDACTED***"'),
        (r'"access_token":\s*"[^"]*"', '"access_token": "***REDACTED***"'),
        (r'sk-[\w-]{30,}', '***REDACTED***'),                 # OpenAI API keys
        (r'ghp_[\w-]+', '***REDACTED***'),                    # GitHub tokens
    ]
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text


class ColorFormatter(logging.Formatter):
    """Colored formatter with TTY detection and secret redaction."""

    COLORS = {
        'DEBUG': '\033[36m',      # Cyan
        'INFO': '\033[32m',       # Green
        'WARNING': '\033[33m',    # Yellow
        'ERROR': '\033[31m',      # Red
        'CRITICAL': '\033[35m',   # Magenta
    }
    RESET = '\033[0m'

    EMOJI = {
        'DEBUG': '🔍',
        'INFO': 'ℹ️ ',
        'WARNING': '⚠️ ',
        'ERROR': '❌',
        'CRITICAL': '🚨',
    }

    def __init__(self, use_color: bool = True, use_emoji: bool = True):
        super().__init__()
        self.use_color = use_color and _is_tty()
        self.use_emoji = use_emoji

    def format(self, record: logging.LogRecord) -> str:
        """Format log record with timestamp, level, emoji, and message."""
        # Timestamp in UTC HH:MM:SS
        dt = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime('%H:%M:%S')

        levelname = record.levelname
        emoji = self.EMOJI.get(levelname, '')

        # Apply color if TTY
        if self.use_color:
            color = self.COLORS.get(levelname, '')
            levelname = f"{color}{levelname}{self.RESET}"

        # Get message with exception info
        msg = record.getMessage()
        if record.exc_info:
            msg += '\n' + logging.Formatter().formatException(record.exc_info)

        # Redact secrets
        msg = _redact_secrets(msg)

        # Format: [HH:MM:SS] EMOJI LEVEL: message
        if self.use_emoji:
            return f"[{dt}] {emoji} {levelname}: {msg}"
        else:
            return f"[{dt}] {levelname}: {msg}"


def setup_logging(name: str = None, level: str = None) -> logging.Logger:
    """
    Setup structured logging with TTY-aware colors and secret redaction.

    Args:
        name: Logger name (default: __name__)
        level: Logging level as string (default: INFO from LOG_LEVEL env var)

    Returns:
        Configured logger instance
    """
    # Determine log level
    if level is None:
        level = os.environ.get("LOG_LEVEL", "INFO").upper()

    try:
        log_level = getattr(logging, level)
    except AttributeError:
        log_level = logging.INFO

    # Create logger
    logger = logging.getLogger(name or __name__)
    logger.setLevel(log_level)
    logger.handlers.clear()
    logger.propagate = False

    # Create handler and formatter
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(log_level)

    formatter = ColorFormatter(use_color=_is_tty(), use_emoji=True)
    handler.setFormatter(formatter)

    logger.addHandler(handler)

    return logger
