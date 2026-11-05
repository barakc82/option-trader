"""Runs one Meitav operation script for a user and program type.

From the repo root, either pass them on the command line (as the PowerShell scripts in
scripts/ do), using the names from meitav_common:
    python -m utilities.meitav.run_operation buy_leveraged Barak Hishtalmut
or edit USER, PROGRAM_TYPE and OPERATION_TYPE below and run with no arguments:
    python -m utilities.meitav.run_operation
"""
import argparse
import importlib

from utilities.meitav.meitav_common import Barak, Mom, Hilush, Hishtalmut, Gemel

BUY_LEVERAGED = 'buy_leveraged'
BUY_REGULAR = 'buy_regular'
SELL_LEVERAGED = 'sell_leveraged'
SELL_REGULAR = 'sell_regular'
AUDIT_BUY_TRANSACTION = 'audit_buy_transaction'
UPDATE_SPREADSHEET = 'update_spreadsheet'

# Operation type -> module in this folder whose main(user, program_type) performs it.
OPERATION_MODULES = {
    BUY_LEVERAGED: 'utilities.meitav.buy_leveraged',
    BUY_REGULAR: 'utilities.meitav.buy_regular',
    SELL_LEVERAGED: 'utilities.meitav.sell_leveraged',
    SELL_REGULAR: 'utilities.meitav.sell_regular',
    AUDIT_BUY_TRANSACTION: 'utilities.meitav.audit_buy_transaction',
    UPDATE_SPREADSHEET: 'utilities.meitav.meitav_update_ss',
}

USERS = {'Barak': Barak, 'Mom': Mom, 'Hilush': Hilush}
PROGRAM_TYPES = {'Hishtalmut': Hishtalmut, 'Gemel': Gemel}

USER = Hilush
PROGRAM_TYPE = Gemel
OPERATION_TYPE = BUY_LEVERAGED


def run_operation(operation_type, user, program_type):
    if operation_type not in OPERATION_MODULES:
        raise ValueError(f"Unknown operation type {operation_type!r}, expected one of {sorted(OPERATION_MODULES)}")
    # Imported only when needed, so the other operations' dependencies aren't loaded.
    module = importlib.import_module(OPERATION_MODULES[operation_type])
    module.main(user, program_type)


def _name_to_constant(kind, name, constants):
    by_lower_name = {constant_name.lower(): value for constant_name, value in constants.items()}
    if name.lower() not in by_lower_name:
        raise SystemExit(f"Unknown {kind} {name!r}, expected one of {sorted(constants)}")
    return by_lower_name[name.lower()]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run a Meitav operation for a user and program type.")
    parser.add_argument('operation_type', nargs='?', choices=sorted(OPERATION_MODULES))
    parser.add_argument('user', nargs='?', help=f"one of {sorted(USERS)}")
    parser.add_argument('program_type', nargs='?', help=f"one of {sorted(PROGRAM_TYPES)}")
    args = parser.parse_args()

    if args.operation_type is None:
        run_operation(OPERATION_TYPE, USER, PROGRAM_TYPE)
    elif args.user is None or args.program_type is None:
        parser.error("pass the user and program type together with the operation type")
    else:
        run_operation(args.operation_type,
                      _name_to_constant('user', args.user, USERS),
                      _name_to_constant('program type', args.program_type, PROGRAM_TYPES))
