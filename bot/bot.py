"""
Main bot class for the Telegram Food Poll Bot.
"""

import logging
import asyncio
from telegram import BotCommand
from telegram.ext import Application
from .config import BOT_TOKEN, setup_logging
from .handlers import setup_handlers

logger = logging.getLogger(__name__)

class FoodPollBot:
    """
    Main bot class that handles the Telegram Food Poll Bot functionality.
    """
    
    def __init__(self):
        """Initialize the bot."""
        self.application = None
        self._setup_logging()
    
    def _setup_logging(self) -> None:
        """Setup logging configuration."""
        setup_logging()
        logger.info("Logging setup completed")
    
    def setup(self) -> None:
        """Setup the bot application and handlers."""
        try:
            async def post_init(app: Application) -> None:
                """Register bot commands so they appear in the Telegram / menu."""
                await app.bot.set_my_commands([
                    BotCommand("start",  "👋 Welcome & instructions"),
                    BotCommand("vongsa", "💳 Pay Vongsa Hourt (KHQR)"),
                    BotCommand("ty",     "💳 Pay Ty Hen (KHQR)"),
                ])
                logger.info("Bot commands registered with Telegram")

            # Create application without job queue to avoid weak reference issues
            self.application = (
                Application.builder()
                .token(BOT_TOKEN)
                .job_queue(None)
                .post_init(post_init)
                .build()
            )

            # Setup handlers
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
            # Ensure an event loop is set for the current thread
            asyncio.set_event_loop(asyncio.new_event_loop())
            # run_polling is a synchronous method that starts its own loop
            self.application.run_polling(drop_pending_updates=True)
        except KeyboardInterrupt:
            logger.info("Bot stopped by user")
        except Exception as e:
            logger.error(f"Error running bot: {e}")
            raise