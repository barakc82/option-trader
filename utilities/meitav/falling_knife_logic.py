import math
import logging

logger = logging.getLogger(__name__)

BOTTOM = 695
TOP = 9281
FEE_RATE = 0.0025 * 3

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
        new_leveraged_sum = leveraged_price * self.leveraged_quantity / 100
        
        if self.should_decrease_base and leveraged_price != self.current_leveraged_price:
            # Use 1/3 power to simulate correlation (leveraged vs base volatility)
            real_decrease_ratio = math.pow(self.current_leveraged_price / leveraged_price, 1/3)
            new_base_sum = sum(q * p / real_decrease_ratio for q, p in self.base_etfs_data) / 100
        else:
            new_base_sum = sum(q * p for q, p in self.base_etfs_data) / 100

        initial_total = new_leveraged_sum + new_base_sum + self.cash
        total_fees = initial_total * FEE_RATE
        cash_for_investment = max(self.cash - total_fees, 0)
        new_total = new_leveraged_sum + new_base_sum + cash_for_investment
        
        new_leveraged_share = new_leveraged_sum / new_total
        base_share = new_base_sum / new_total
        
        status = (leveraged_price - BOTTOM) / (TOP - BOTTOM)
        required_leveraged_share = self.min_leveraged_share + (self.max_leveraged_share - self.min_leveraged_share) * (1 - status)
        required_transfer = (required_leveraged_share - new_leveraged_share) * new_total

        return {
            "leveraged_price": leveraged_price,
            "new_leveraged_sum": new_leveraged_sum,
            "new_base_sum": new_base_sum,
            "new_total": new_total,
            "new_leveraged_share": new_leveraged_share,
            "base_share": base_share,
            "cash_for_investment": cash_for_investment,
            "status": status,
            "required_leveraged_share": required_leveraged_share,
            "required_transfer": required_transfer
        }

    def find_next_transfer_price(self):
        """Searches for the price boundary where a transfer of 1000 NIS is required."""
        current_result = self.calculate_at_price(self.current_leveraged_price)
        should_buy_now = current_result['required_transfer'] > 1000

        if should_buy_now:
            # Simulate buying 1000 NIS at a time until rebalanced, then find the next lower trigger.
            # After each buy the leveraged share rises, so the next trigger is higher than it would be
            # without accounting for the purchase.
            simulated_qty = self.leveraged_quantity
            simulated_cash = self.cash
            person_data = {'max_leveraged_share': self.max_leveraged_share, 'min_leveraged_share': self.min_leveraged_share}

            while simulated_cash >= 1000:
                temp_calc = FallingKnifeCalculator(
                    simulated_qty, self.current_leveraged_price,
                    self.base_etfs_data, simulated_cash,
                    person_data, self.should_decrease_base
                )
                if temp_calc.calculate_at_price(self.current_leveraged_price)['required_transfer'] <= 1000:
                    break
                # Buy 1000 NIS worth: price is in agorot, so units = 100000 / price
                simulated_qty += 100000 / self.current_leveraged_price
                simulated_cash -= 1000
            else:
                # Cash exhausted — create a calc with the final simulated state
                temp_calc = FallingKnifeCalculator(
                    simulated_qty, self.current_leveraged_price,
                    self.base_etfs_data, simulated_cash,
                    person_data, self.should_decrease_base
                )

            for price in range(self.current_leveraged_price - 1, 0, -1):
                result = temp_calc.calculate_at_price(price)
                if result['required_transfer'] > 1000:
                    return result['leveraged_price'], result
        else:
            # Not yet in buy territory, search DOWN for the boundary where we start buying
            for price in range(self.current_leveraged_price - 1, 0, -1):
                result = self.calculate_at_price(price)
                if result['required_transfer'] > 1000:
                    return result['leveraged_price'], result

        return self.current_leveraged_price, current_result

    def print_summary(self, target_price, result):
        """Utility to print the simulation results in a standardized format."""
        print(f"Target price for transfer: {target_price}")
        print(f"At target price, Status:\t{result['status']:.4f}\n"
              f"New leveraged sum:\t{result['new_leveraged_sum']:.2f}\n"
              f"New base sum:\t{result['new_base_sum']:.2f}\n"
              f"Cash for investment:\t{result['cash_for_investment']:.2f}\n"
              f"New total:\t{result['new_total']:.2f}\n"
              f"Required leveraged share:\t{result['required_leveraged_share']:.2f}\n"
              f"New leveraged share:\t{result['new_leveraged_share']:.2f}\n"
              f"Required transfer:\t{result['required_transfer']:.2f}")
        
        units = math.ceil(100000 / target_price) if target_price > 0 else 0
        print(f"Units to buy: {units}")
        return units
