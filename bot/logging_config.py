"""Logging configuration for the FPL Discord bot."""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Ensure logs directory exists
LOGS_DIR = Path("logs")
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# Create logger
logger = logging.getLogger('fpl_bot')
logger.setLevel(logging.DEBUG)

# Prevent duplicate handlers if module is reloaded
if not logger.handlers:
    # Console handler - INFO level and above
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_format = logging.Formatter(
        '%(asctime)s | %(levelname)-8s | %(message)s',
        datefmt='%H:%M:%S'
    )
    console_handler.setFormatter(console_format)
    logger.addHandler(console_handler)

    # File handler - DEBUG level and above with rotation
    file_handler = RotatingFileHandler(
        LOGS_DIR / 'fpl_bot.log',
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setLevel(logging.DEBUG)
    file_format = logging.Formatter(
        '%(asctime)s | %(levelname)-8s | %(name)s | %(funcName)s:%(lineno)d | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    file_handler.setFormatter(file_format)
    logger.addHandler(file_handler)

    # Error file handler - ERROR level and above
    error_handler = RotatingFileHandler(
        LOGS_DIR / 'fpl_bot_errors.log',
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=3,
        encoding='utf-8'
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(file_format)
    logger.addHandler(error_handler)


def get_logger(name: str = None) -> logging.Logger:
    """Get a logger instance. If name is provided, creates a child logger."""
    if name:
        return logger.getChild(name)
    return logger
