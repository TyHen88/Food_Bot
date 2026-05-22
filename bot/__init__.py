"""
Telegram Food Poll Bot Package

A Telegram bot that helps users select food from a menu using interactive polls.
"""

__version__ = "1.0.0"
__author__ = "Henty"
__description__ = "Telegram bot for food menu polling and ordering"

from .bot import build_application

__all__ = ["build_application"]
