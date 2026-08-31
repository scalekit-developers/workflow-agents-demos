"""
Structured logging with TTY-aware colors and secret + PII redaction.

Supports:
  - TTY detection (auto-disable colors in CI/pipes)
  - LOG_LEVEL env var (DEBUG/INFO/WARNING/ERROR)
  - Redaction of secrets (Scalekit credentials, bearer tokens, API keys)
  - Redaction of bank account / routing number shaped values
  - Timestamp + level + message format

This agent handles payroll and bank/direct-deposit data, which is one of the
most sensitive categories of PII an agent in this workspace touches. Beyond
the standard secret-redaction patterns used by every other agent here (Scalekit
client IDs, client secrets, bearer tokens), this module ALSO redacts anything
that merely looks like a bank account number or routing number, even if it
appears in a log line that was never intended to contain one. This is a
defense-in-depth measure: application code (connectors.py, aggregator.py,
run_flow.py) is written to never pass a full unmasked account/routing number
into a log message in the first place (only masked forms like "****1234" are
ever logged), but this redaction layer exists as a second, independent line of
defense in case of a future bug, a raw exception message from a connector that
happens to echo back input, or a copy-paste mistake -- the same reasoning
Scalekit's own `skc_`/`test_` patterns exist for credentials.

New patterns added for this agent (see _redact_secrets below):
  - `_ROUTING_NUMBER_PATTERN`: exactly 9 consecutive digits, the fixed length
    of a US ABA routing number. Redacted unconditionally -- there is no
    legitimate reason a 9-digit run needs to appear in a log line for this
    agent, so this pattern is intentionally broad rather than trying to also
    checksum-validate before redacting (a checksum-valid 9-digit run and a
    checksum-invalid one are equally sensitive-looking and both get masked).
  - `_BANK_ACCOUNT_NUMBER_PATTERN`: 8 to 17 consecutive digits (the practical
    range of US bank account number lengths), matched only when NOT already
    part of a longer digit run that would itself be caught by other rules and
    only when it looks like a standalone token (word boundary on both sides),
    to avoid over-redacting incidental long numbers like timestamps or
    Scalekit resource IDs (which are alphanumeric with hyphens/underscores,
    not bare decimal runs, and are handled by their own patterns above).
  - `_MASKED_ACCOUNT_LABEL_PATTERN` is NOT redacted -- values like
    "****1234" or "ending in 1234" are the intentionally-safe masked form
    this codebase logs instead of the real number, and must remain visible
    for the log to be useful for audits and debugging.
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
    """Replace common secrets and bank-detail-shaped values with ***REDACTED***."""
    if not isinstance(text, str):
        return text
    patterns = [
        # --- Scalekit / generic credential patterns (same as other agents in this workspace) ---
        (r'sk[_-][\w-]{20,}', '***REDACTED***'),
        (r'skc_[\w-]+', '***REDACTED***'),
        (r'test_[\w-]{20,}', '***REDACTED***'),
        (r'Bearer\s+[\w-]+', 'Bearer ***REDACTED***'),
        (r'Authorization:\s+[\w-]+', 'Authorization: ***REDACTED***'),
        (r'"token":\s*"[^"]*"', '"token": "***REDACTED***"'),
        (r'"access_token":\s*"[^"]*"', '"access_token": "***REDACTED***"'),
        (r'"api_key":\s*"[^"]*"', '"api_key": "***REDACTED***"'),
        # --- Bank/payroll PII patterns (specific to this agent) ---
        # US ABA routing numbers are exactly 9 digits. Redact unconditionally:
        # there is no legitimate reason a bare 9-digit run needs to reach a log
        # line in this agent.
        (r'(?<!\d)\d{9}(?!\d)', '***REDACTED-ROUTING***'),
        # Bank account numbers: 8-17 consecutive digits (the practical range
        # for US bank accounts), matched as a standalone token so it does not
        # also swallow things like ISO timestamps embedded in longer strings.
        (r'(?<!\d)\d{8,17}(?!\d)', '***REDACTED-ACCOUNT***'),
        # Explicit key=value / JSON-field forms, in case a future field name
        # like "new_value" or "account_number" ever gets logged directly.
        (r'"(account_number|routing_number|bank_account|aba_number)":\s*"[^"]*"',
         r'"\1": "***REDACTED***"'),
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
    same formatting, level, and redaction via normal logger propagation,
    instead of only the single named logger returned here. Without this,
    per-module loggers default to the logging library's built-in WARNING
    level with no handler, and INFO-level messages (e.g. "connector -- ACTIVE"
    confirmations) would silently never print.

    Returns a logger configured with:
      - LOG_LEVEL env var (default INFO)
      - TTY-aware colors (auto-disabled in CI)
      - Timestamps in HH:MM:SS format
      - Secret AND bank-detail redaction (see _redact_secrets docstring)
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
