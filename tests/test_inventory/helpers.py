"""
Shared fixtures and builders for the inventory accounting tests.
"""

from datetime import date
from decimal import Decimal

import agate

COLUMN_NAMES = [
    'id', 'product', 'location', 'business_date', 'sequence',
    'direction', 'quantity', 'amount', 'currency',
]

COLUMN_TYPES = [
    agate.Text(), agate.Text(), agate.Text(), agate.Date(), agate.Number(),
    agate.Text(), agate.Number(), agate.Number(), agate.Text(),
]


def make_table(rows, column_names=None, column_types=None):
    """
    Build an agate movement table from raw tuples, keeping native
    Decimal/date values instead of letting agate infer types.
    """
    return agate.Table(
        rows,
        column_names or COLUMN_NAMES,
        column_types or COLUMN_TYPES,
    )


def movement(movement_id, product, location, business_date, sequence,
             direction, quantity, amount, currency='USD'):
    return (
        movement_id, product, location, business_date, Decimal(sequence),
        direction, Decimal(str(quantity)),
        None if amount is None else Decimal(str(amount)), currency,
    )


def simple_rows():
    """
    One product/location, two receipts and two issues, spanning two months.

    Cost story (minor unit = cents):

    * M1: receipt 10 @ 100.00 -> on hand 10, value 100.00, unit 10.00
    * M2: issue 4 @ avg 10.00 -> cogs 40.00, on hand 6, value 60.00
    * M3: receipt 10 @ 110.00 -> on hand 16, value 170.00, unit 10.625
    * M4: issue 16 (empties stock) -> cogs 170.00, zero residual
    """
    return [
        movement('M1', 'WIDGET', 'A1', date(2026, 1, 3), 1, 'receipt', 10, '100.00'),
        movement('M2', 'WIDGET', 'A1', date(2026, 1, 10), 2, 'issue', 4, '80.00'),
        movement('M3', 'WIDGET', 'A1', date(2026, 1, 20), 3, 'receipt', 10, '110.00'),
        movement('M4', 'WIDGET', 'A1', date(2026, 2, 5), 4, 'issue', 16, '360.00'),
    ]


def payload(movement_id, product, location, business_date, sequence,
            direction, quantity, amount, currency='USD'):
    """A correction payload using the default config column names."""
    return {
        'id': movement_id,
        'product': product,
        'location': location,
        'business_date': business_date,
        'sequence': Decimal(sequence),
        'direction': direction,
        'quantity': Decimal(str(quantity)),
        'amount': None if amount is None else Decimal(str(amount)),
        'currency': currency,
    }


def rows_by_id(table):
    return {row['movement_id']: row for row in table.rows}


def balances_by_key(table):
    return {(row['product'], row['location']): row for row in table.rows}
