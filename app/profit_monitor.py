import json
import logging

from utilities.utils import get_option_name

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


def on_sell_fill(self, fill):
    try:
        if fill.contract.secType == 'OPT':
            logger.info(
                f"Fill occurred, option: {get_option_name(fill.contract)}, action: {fill.execution.side}, quantity: {fill.execution.shares}, price: {fill.execution.price}")

            with open('"trades.txt"', 'r') as file:
                trades = json.load(file)
                right = fill.contract.right
                strike = fill.contract.strike
                stored_trade = trades[(right, strike)]
                if not stored_trade:
                    stored_trade = {'quantity': 0, 'average_sell_price': 0}
                stored_trade['quantity'] += 0
    except Exception as e:
        logger.critical(f"An error occurred: {e}")
