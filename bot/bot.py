"""
Main bot class for the Telegram Food Poll Bot.
"""

import asyncio
import logging

from telegram import BotCommand
from telegram.ext import Application

from .config import BOT_TOKEN, setup_logging
from .handlers import setup_handlers
from .scheduler import setup_scheduler

logger = logging.getLogger(__name__)


class FoodPollBot:
    """Main bot class that handles Telegram bot orchestration."""

    def __init__(self):
        self.application = None
        self._setup_logging()

    def _setup_logging(self) -> None:
        """Setup logging configuration."""
        setup_logging()
        logger.info("Logging setup completed")

    def setup(self) -> None:
        """Setup the bot application, scheduler, and handlers."""
        try:
            async def post_init(app: Application) -> None:
                await app.bot.set_my_commands(
                    [
                        BotCommand("start", "Welcome & instructions"),
                        BotCommand("subscribe", "Subscribe this chat to reminders"),
                        BotCommand("unsubscribe", "Unsubscribe this chat from reminders"),
                        BotCommand("vongsa", "Pay Vongsa Hourt (KHQR)"),
                        BotCommand("ty", "Pay Ty Hen (KHQR)"),
                    ]
                )
                await setup_scheduler(app)
                logger.info("Bot commands and scheduler registered")

            # Disable PTB JobQueue due Python 3.14 weakref issue in PTB 20.1.
            # Reminders are handled by APScheduler in bot.scheduler.
            self.application = (
                Application.builder()
                .token(BOT_TOKEN)
                .job_queue(None)
                .post_init(post_init)
                .build()
            )

            setup_handlers(self.application)
            logger.info("Bot setup completed successfully")

        except Exception as e:
            logger.error(f"Failed to setup bot: {e}")
            raise

    def run(self) -> None:
        """Run the bot, ensuring an event loop exists for run_polling."""
        if not self.application:
            raise RuntimeError("Bot not setup. Call setup() first.")
        try:
            logger.info("Starting bot...")
            asyncio.set_event_loop(asyncio.new_event_loop())
            self.application.run_polling(drop_pending_updates=True)
        except KeyboardInterrupt:
            logger.info("Bot stopped by user")
        except Exception as e:
            logger.error(f"Error running bot: {e}")
            raise
