import logging

logger = logging.getLogger(__name__)


class RiskyPositionsMonitor:
    def __init__(self, trading_bot):
        self.trading_bot = trading_bot

    def monitor(self, max_loss):
        risky_positions = self.trading_bot.find_risky_options(max_loss=max_loss)
        for risky_position in risky_positions:
            logger.warning(f"Risky option position: {risky_position.contract.right} {risky_position.contract.strike}")
        else:
            logger.info("No risky positions found")
