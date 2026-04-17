"""
JobWingman — application-wide logging setup.

Usage in any module:
    from logger import get_logger
    logger = get_logger(__name__)

    logger.debug("detailed trace — only visible at LOG_LEVEL=DEBUG")
    logger.info("normal operational message")
    logger.warning("something unexpected, but recoverable")
    logger.error("something failed — needs attention")

Setting the log level at runtime:
    LOG_LEVEL=DEBUG  uvicorn main:app ...   # full trace output
    LOG_LEVEL=INFO   uvicorn main:app ...   # default — operational messages
    LOG_LEVEL=WARNING uvicorn main:app ...  # warnings and errors only
"""

import logging
import os

# ---------------------------------------------------------------------------
# Internal constants
# ---------------------------------------------------------------------------

# Name of the environment variable that controls the application log level.
_LOG_LEVEL_ENV_VAR = "LOG_LEVEL"

# Fallback level used when LOG_LEVEL is not set or is unrecognised.
_DEFAULT_LOG_LEVEL = "INFO"

# Root namespace for all application loggers.  All child loggers inherit
# level + handler from this root so a single handler is sufficient.
_APP_ROOT = "jobwingman"

# Log line format:  timestamp  LEVEL     [module]  message
_LOG_FORMAT = "%(asctime)s  %(levelname)-8s  [%(name)s]  %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


def _setup() -> logging.Logger:
    """
    Configure the root application logger and return it.

    Reads LOG_LEVEL from the environment (case-insensitive).  Unrecognised
    values fall back to INFO with a printed warning — print() is intentional
    here because the logger itself is not ready yet.

    Guarded by `if not root.handlers` so re-importing this module in tests
    or on hot-reload does not duplicate the handler.
    """
    level_name = os.environ.get(_LOG_LEVEL_ENV_VAR, _DEFAULT_LOG_LEVEL).upper()
    level = getattr(logging, level_name, None)

    if level is None:
        print(f"[logger] Unknown LOG_LEVEL={level_name!r} — falling back to INFO")
        level = logging.INFO

    root = logging.getLogger(_APP_ROOT)
    root.setLevel(level)

    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setLevel(level)
        handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
        root.addHandler(handler)

    return root


# Module-level singleton — configured once when this module is first imported.
# Modules that want the root app logger can import this directly:
#   from logger import logger
logger = _setup()


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------


def get_logger(name: str) -> logging.Logger:
    """
    Return a child logger under the 'jobwingman' namespace.

    Args:
        name: Typically pass __name__ from the calling module.
              pipeline/orchestrator.py → jobwingman.pipeline.orchestrator
              llm/gemini/client.py    → jobwingman.llm.gemini.client
              job_sources/arbeitnow.py → jobwingman.job_sources.arbeitnow

    The returned logger inherits level and handler from the root app logger.
    Its fully-qualified name appears in every log line, making it trivial to
    trace which module emitted a message without reading the code.
    """
    return logging.getLogger(f"{_APP_ROOT}.{name}")
