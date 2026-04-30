import asyncio
import random
import logging
import json
import os
import sys
import math
from datetime import date, datetime, timedelta

from utilities.utils import *
from utilities.ib_utils import *
from .account_data import AccountData
from .market_data_fetcher import MarketDataFetcher
from .max_loss_calculator import calculate_max_loss, MaxLossCalculator
from .option_cache import OptionCache
from .strike_finder import StrikeFinder
from .target_delta_calculator import TargetDeltaCalculator
from .connection_manager import ConnectionManager
from .trading_bot import TradingBot

TIME_UNTIL_NEXT_SELL_CHECK = 120
LOWER_MINIMAL_SELL_PRICE_TO_CLOSE_POSITION = MINIMAL_SELL_PRICE + 0.05
HIGHER_MINIMAL_SELL_PRICE_TO_CLOSE_POSITION = MINIMAL_SELL_PRICE + 0.1

logger = logging.getLogger(__name__)

class OpportunityExplorer:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(OpportunityExplorer, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self.ib = ConnectionManager().ib
            self.account_data = AccountData()
            self.market_data_fetcher = MarketDataFetcher()
            self.trading_bot = TradingBot()
            self.last_submit_order_attempt_time = 0
            self.should_cancel_all_sell_orders = False
            self.no_put_options_above_minimal_sell_price = False
            self.no_call_options_above_minimal_sell_price = False
            self.can_submit_orders = True
            self.last_put_option_price = 0
            self.last_call_option_price = 0
            
            self.should_write_options_overnight = True
            self.should_monitor_only = False
            self._initialized = True

    def load_config(self):
        config_path = "config/option_trader_config.json"
        try:
            if os.path.exists(config_path):
                with open(config_path, "r") as f:
                    config = json.load(f)
                    self.should_write_options_overnight = config.get("should_write_options_overnight", True)
                    self.should_monitor_only = config.get("should_monitor_only", False)
        except Exception as e:
            logger.error(f"OpportunityExplorer: Error reading config: {e}")

    async def explore_opportunities(self):
        self.load_config()
        logger.info("Exploring new opportunities")
        
        current_date = get_current_trading_day()
        options_cache = OptionCache(self.market_data_fetcher)
        options = await options_cache.load(current_date)
        open_trades = await self.trading_bot.get_open_trades()
        
        now = time.time()
        self.can_submit_orders = (now - self.last_submit_order_attempt_time > TIME_UNTIL_NEXT_SELL_CHECK)
        if is_switched_to_overnight_trading():
            self.can_submit_orders = self.should_write_options_overnight
        self.can_submit_orders &= not self.should_monitor_only

        if self.can_submit_orders:
            sell_put_result = await self._try_to_sell_side(open_trades, options, 'P')
            sell_call_result = await self._try_to_sell_side(open_trades, options, 'C')

            if sell_call_result.success or sell_put_result.success:
                self.last_submit_order_attempt_time = now
            self.should_cancel_all_sell_orders = sell_call_result.is_low_projected_cushion or sell_put_result.is_low_projected_cushion
        else:
            next_check = self.last_submit_order_attempt_time + TIME_UNTIL_NEXT_SELL_CHECK
            logger.info(f"Next sell check at {datetime.fromtimestamp(next_check).strftime('%H:%M')}")

    async def _try_to_sell_side(self, open_trades, options, right):
        side_options = [o for o in options if o.right == right]
        result = SellOptionResult()
        if not side_options:
            logger.error(f"No {right} options found")
            return result

        target_delta = await TargetDeltaCalculator().calculate_target_delta()
        strike_finder = StrikeFinder()
        option = await (strike_finder.get_low_delta_put_option(side_options, target_delta) if right == 'P' 
                        else strike_finder.get_low_delta_call_option(side_options, target_delta))
        
        if not option:
            logger.error(f"{right} option candidate not found")
            return result

        last_price = extract_last_median_price(option.ticker)
        if right == 'P':
            if self.last_put_option_price != last_price and not math.isnan(last_price):
                logger.info(f"Put price level changed: {self.last_put_option_price} -> {last_price}")
                self.last_put_option_price = last_price
        else:
            if self.last_call_option_price != last_price and not math.isnan(last_price):
                logger.info(f"Call price level changed: {self.last_call_option_price} -> {last_price}")
                self.last_call_option_price = last_price

        stop_loss_limit = await calculate_max_loss(right, should_consider_only_effective=True)
        if stop_loss_limit < last_price:
            logger.warning(f"Aborting {right} sell: acceptable loss {stop_loss_limit} < price {last_price}")
            return result

        max_qty = await self._calculate_max_options_scenario(option)
        if max_qty == 0:
            logger.warning(f"Aborting {right} sell due to projected exposure fee risk")
            return result

        self._cancel_matching_buy_trades(open_trades, option)
        await asyncio.sleep(0.2)

        quantity = min(max_qty, 2)
        result = await self._execute_sell(option, quantity, target_delta)
        
        if not result.success:
            logger.warning(f"Failed to sell {quantity} {right} options. Trying 1...")
            result = await self._execute_sell(option, 1, target_delta)
            if not result.success:
                logger.warning(f"Failed to sell 1 {right} option.")
                if result.required_initial_margin and not is_switched_to_overnight_trading():
                    await self._try_to_reduce_initial_margin(option, result, side_options)

        if right == 'P': self.no_put_options_above_minimal_sell_price = result.no_option_above_minimal_sell_price
        else: self.no_call_options_above_minimal_sell_price = result.no_option_above_minimal_sell_price
        
        return result

    async def _execute_sell(self, option, quantity, target_delta):
        delta = abs(get_delta(option.ticker))
        if delta > target_delta:
            logger.warning(f"Aborting sell: delta {delta:.3f} > target {target_delta:.3f}")
            return SellOptionResult()

        start = time.time()
        result = await self.trading_bot.try_to_sell(option, quantity)
        duration = time.time() - start

        new_delta = abs(get_delta(option.ticker))
        if result.success:
            if new_delta > target_delta:
                logger.warning(f"Sold {get_option_name(option)}, but delta {new_delta:.3f} > target {target_delta:.3f}. Duration: {duration:.2f}s")
            req_id_to_comment[result.trade.order.orderId] = f"Delta: {new_delta:.3f}, Target: {target_delta:.3f}"
            req_id_to_target_delta[result.trade.order.orderId] = target_delta
        return result

    async def _calculate_max_options_scenario(self, option):
        """Generic risk calculation for market rise (Calls) or drop (Puts)."""
        if not is_switched_to_overnight_trading():
            return sys.float_info.max

        right = option.right
        # Rise/Drop parameters
        fraction = 1.2 if right == 'C' else 0.7  # +20% for calls, -30% for puts
        
        current_price = await self.market_data_fetcher.get_spx_price()
        if math.isnan(current_price):
            logger.error(f"SPX price is NaN, cannot calculate {right} risk scenario")
            return 0

        target_price = current_price * fraction
        
        # Immediate safety check
        if (right == 'C' and option.strike > target_price) or (right == 'P' and option.strike < target_price):
            return sys.float_info.max

        positions = await self.trading_bot.get_short_options()
        total_liability = 0
        for p in positions:
            if p.contract.secType != 'OPT' or p.position >= 0 or p.contract.right != right:
                continue
            
            # Skip if strike is beyond our risk boundary
            if (right == 'C' and p.contract.strike > target_price) or (right == 'P' and p.contract.strike < target_price):
                continue
                
            expiry = datetime.strptime(p.contract.lastTradeDateOrContractMonth, "%Y%m%d").date()
            if expiry <= date.today(): continue
            
            liability = abs(target_price - p.contract.strike) * 100 * abs(p.position)
            total_liability += liability

        net_liq = await self.account_data.get_net_liquidation_value()
        net_liq_after = net_liq * (fraction if right == 'P' else 1.0) # Net liq drops with market drop
        
        net_worth_after = net_liq_after - total_liability
        if net_worth_after < 0:
            logger.info(f"Negative projected net worth for {right} scenario")
            return 0

        if target_price == option.strike: return sys.float_info.max
        
        liability_per_contract = abs(target_price - option.strike) * 100
        return math.floor(net_worth_after / liability_per_contract)

    def _cancel_matching_buy_trades(self, open_trades, option):
        for trade in open_trades:
            if trade.contract.conId == option.conId and trade.order.action.upper() == 'BUY':
                logger.info(f"Cancelling existing buy order for {get_option_name(option)} to allow sell")
                self.trading_bot.cancel_trade(trade)

    async def _try_to_reduce_initial_margin(self, option_to_sell, sell_result, side_options):
        right = option_to_sell.right
        min_price_to_close = await self._calc_min_close_price(right)
        
        last_price = self.last_put_option_price if right == 'P' else self.last_call_option_price
        if last_price <= min_price_to_close:
            return

        strike_finder = StrikeFinder()
        positions = await self.trading_bot.get_short_options()
        
        if right == 'C':
            limit_strike = min(p.contract.strike for p in positions) if positions else 0
            cheap_option = await strike_finder.get_available_cheap_call_option(side_options, limit_strike)
        else:
            limit_strike = max(p.contract.strike for p in positions) if positions else 99999
            cheap_option = await strike_finder.get_available_cheap_put_option(side_options, limit_strike)

        if not cheap_option: return

        im_change = await self.trading_bot.get_initial_margin_change(cheap_option, 1)
        if im_change <= 0: return

        missing = sell_result.required_initial_margin - sell_result.initial_margin_after
        qty = math.ceil(missing / im_change)
        
        if qty > 0 and (last_price * 0.4 > 0.07 * qty + 0.02):
            logger.info(f"Relaxing margin: buying {qty} units of {get_option_name(cheap_option)}")
            trade = self.trading_bot.buy_low_cost(cheap_option, qty)
            req_id_to_comment[trade.order.orderId] = "Margin relax"
            self.can_submit_orders = True

    async def _calc_min_close_price(self, right):
        positions = await self.trading_bot.get_short_options()
        current_count = len([p for p in positions if p.contract.right == right])
        max_opts = MaxLossCalculator().get_max_number_of_options(right)
        
        vacant_fraction = 1 - (current_count / max_opts if max_opts > 0 else 0)
        elapsed_fraction = get_elapsed_day_fraction()
        
        if random.random() < (vacant_fraction * elapsed_fraction):
            return LOWER_MINIMAL_SELL_PRICE_TO_CLOSE_POSITION
        return HIGHER_MINIMAL_SELL_PRICE_TO_CLOSE_POSITION

def find_all_buy_trades(option, open_buy_trades):
    return [t for t in open_buy_trades if t.contract.conId == option.conId and t.order.action.upper() == 'BUY']
