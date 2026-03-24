import logging
import sys
from datetime import datetime, timezone


COLORS = {
    "DEBUG": "\033[90m",      # grey
    "INFO": "\033[92m",       # green
    "WARNING": "\033[93m",    # yellow
    "ERROR": "\033[91m",      # red
    "CRITICAL": "\033[95m",   # magenta
    "TRADE": "\033[96m",      # cyan
}
RESET = "\033[0m"

TRADE_LEVEL = 25
logging.addLevelName(TRADE_LEVEL, "TRADE")


class ColorFormatter(logging.Formatter):
    """Colored log formatter with timestamps and source labels."""

    def format(self, record: logging.LogRecord) -> str:
        level = record.levelname
        color = COLORS.get(level, "")
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
        name = record.name
        msg = record.getMessage()
        return f"{color}[{ts}] [{level:<7}] [{name}] {msg}{RESET}"


def get_logger(name: str) -> logging.Logger:
    """Create a colored logger with the given name."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(ColorFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False

    def trade(msg: str, *args, **kwargs):
        logger.log(TRADE_LEVEL, msg, *args, **kwargs)

    logger.trade = trade  # type: ignore[attr-defined]
    return logger
