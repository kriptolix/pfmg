
import logging
import os


def get_logger(name: str) -> logging.Logger:
    """logging — consistent logger factory."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        fmt = "%(levelname)-8s %(name)s — %(message)s"
        handler.setFormatter(logging.Formatter(fmt))
        logger.addHandler(handler)
    level = os.environ.get("pfmg_LOG_LEVEL", "INFO").upper()
    logger.setLevel(getattr(logging, level, logging.INFO))
    return logger