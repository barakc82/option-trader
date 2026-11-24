import math
import logging
from typing import Any

logger = logging.getLogger(__name__)

BOTTOM = 695
TOP = 9281
FEE_RATE = 0.0025 * 3
# find_next_transfer_price skips prices whose required transfer is below this sum.
MIN_TRANSFER_IN_SHEKELS = 1000

class FallingKnifeCalculator:
    def __init__(self, leveraged_quantity, current_leveraged_price, base_etfs_data, cash, person_data, should_decrease_base=True):
        """
        :param leveraged_quantity: Current quantity of leveraged ETF
        :param current_leveraged_price: Baseline price for correlation calculations
        :param base_etfs_data: List of (quantity, price) tuples for base ETFs
        :param cash: Current cash balance
        :param person_data: Dict containing max_leveraged_share and min_leveraged_share
        :param should_decrease_base: Whether to scale base ETF prices with market drops
        """
        self.leveraged_quantity = leveraged_quantity
        self.current_leveraged_price = current_leveraged_price
        self.base_etfs_data = base_etfs_data
        self.cash = cash
        self.max_leveraged_share = person_data['max_leveraged_share']
        self.min_leveraged_share = person_data['min_leveraged_share']
        self.should_decrease_base = should_decrease_base

    def calculate_at_price(self, leveraged_price):
        """Simulates portfolio state at a specific leveraged price point."""
        leveraged_sum_in_shekels = leveraged_price * self.leveraged_quantity / 100
        
        if self.should_decrease_base and leveraged_price != self.current_leveraged_price:
            # Use 1/3 power to simulate correlation (leveraged vs base volatility)
            real_decrease_ratio = math.pow(self.current_leveraged_price / leveraged_price, 1/3)
            new_base_sum_in_shekels = sum(q * p / real_decrease_ratio for q, p in self.base_etfs_data) / 100
        else:
            new_base_sum_in_shekels = sum(q * p for q, p in self.base_etfs_data) / 100

        initial_total = leveraged_sum_in_shekels + new_base_sum_in_shekels + self.cash
        total_fees = initial_total * FEE_RATE
        cash_for_investment = max(self.cash - total_fees, 0)
        total_in_shekels = leveraged_sum_in_shekels + new_base_sum_in_shekels + cash_for_investment
        
        leveraged_share = leveraged_sum_in_shekels / total_in_shekels
        base_share = new_base_sum_in_shekels / total_in_shekels
        
        status = (leveraged_price - BOTTOM) / (TOP - BOTTOM)
        required_leveraged_share = self.calculate_required_leveraged_share(status)
        print(f"Required leveraged share: {required_leveraged_share:.3f}, Leveraged share: {leveraged_share:.3f}, total in shekels: {total_in_shekels:.0f}")
        required_transfer = (required_leveraged_share - leveraged_share) * total_in_shekels

        return {
            "leveraged_price": leveraged_price,
            "leveraged_sum_in_shekels": leveraged_sum_in_shekels,
            "new_base_sum_in_shekels": new_base_sum_in_shekels,
            "total_in_shekels": total_in_shekels,
            "leveraged_share": leveraged_share,
            "base_share": base_share,
            "cash_for_investment": cash_for_investment,
            "status": status,
            "required_leveraged_share": required_leveraged_share,
            "required_transfer": required_transfer
        }

    def calculate_required_leveraged_share(self, status: Any) -> Any:
        return self.min_leveraged_share + (self.max_leveraged_share - self.min_leveraged_share) * (1 - status)

    def find_next_transfer_price(self):
        """Returns (price, result) for the highest leveraged price, at or below the current
        one (in agorot), at which the leveraged share of the portfolio is lower than its
        required share by at least MIN_TRANSFER_IN_SHEKELS, along with calculate_at_price's
        result at that price. Prices where the shortfall is smaller are skipped. When the
        shortfall at the current price is already big enough, that's the current price.

        As the price falls, the leveraged share falls and the required share rises, so the
        search walks down one agora at a time from the current price and stops at the
        first price that qualifies."""
        for price in range(self.current_leveraged_price, 0, -1):
            result = self.calculate_at_price(price)
            if (result['leveraged_share'] < result['required_leveraged_share']
                    and result['required_transfer'] >= MIN_TRANSFER_IN_SHEKELS):
                return price, result

        raise ValueError(f"The required transfer doesn't reach {MIN_TRANSFER_IN_SHEKELS} NIS at any price at or "
                         f"below {self.current_leveraged_price}")

    @staticmethod
    def calculate_units(result, cash):
        """Number of units that the required transfer of result (in NIS, as returned by
        calculate_at_price) buys at its leveraged price (in agorot). The transfer is capped
        at cash (in NIS): the required transfer is rounded up to whole units, but never to
        more units than cash buys. 0 when no transfer is required."""
        price = result['leveraged_price']
        required_transfer = result['required_transfer']
        if price <= 0 or required_transfer <= 0:
            return 0
        required_units = math.ceil(required_transfer * 100 / price)
        affordable_units = math.floor(cash * 100 / price)
        print(f"barak: available cash: {cash}")
        if required_units > affordable_units:
            print(f"The required transfer of {required_transfer:.2f} NIS is more than the cash of "
                  f"{cash:.2f} NIS allows, capping it at {affordable_units} units")
            return affordable_units
        return required_units

    def print_summary(self, target_price, result, units):
        """Utility to print the simulation results in a standardized format."""
        print(f"Target price for transfer: {target_price}")
        print(f"At target price, Status:\t{result['status']:.4f}\n"
              f"Leveraged sum in shekels:\t{result['leveraged_sum_in_shekels']:.2f}\n"
              f"New base sum in shekels:\t{result['new_base_sum_in_shekels']:.2f}\n"
              f"Cash for investment:\t{result['cash_for_investment']:.2f}\n"
              f"Total in shekels:\t{result['total_in_shekels']:.2f}\n"
              f"Required leveraged share:\t{result['required_leveraged_share']:.2f}\n"
              f"Leveraged share:\t{result['leveraged_share']:.2f}\n"
              f"Required transfer:\t{result['required_transfer']:.2f}")
        print(f"Units to buy: {units}")
