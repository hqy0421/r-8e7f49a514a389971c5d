"""
Correctable perpetual moving-average inventory accounting.

This subpackage turns an immutable agate movement table (goods receipts and
issues, keyed by product and storage location) into a versioned ledger of
inventory costs and gross margin.

Typical use::

    import agate

    movements = agate.Table.from_csv('movements.csv', column_types={
        'business_date': agate.Date(date_format='%Y-%m-%d'),
        'sequence': agate.Number(),
        'quantity': agate.Number(),
        'amount': agate.Number(),
    })

    ledger = agate.inventory.InventoryLedger(movements)
    snapshot = ledger.latest_snapshot

    snapshot.cost_detail.print_table()
    snapshot.balances.print_table()
    snapshot.monthly_profit.print_table()

    # A late receipt or an amount correction is a new, fully validated
    # snapshot; the old one and its contributions remain auditable.
    corrected = ledger.insert_movement({
        'id': 'M99',
        'product': 'WIDGET',
        'location': 'A1',
        'business_date': datetime.date(2026, 1, 5),
        'sequence': 5,
        'direction': 'receipt',
        'quantity': decimal.Decimal('10'),
        'amount': decimal.Decimal('120.00'),
        'currency': 'USD',
    })

Design rules:

* Receipts and issues of one product/location are processed in business
  order ``(date, sequence)``; issue cost is the moving average of the stock
  currently on hand.
* Every monetary value is a :class:`decimal.Decimal` quantized to the
  currency's smallest unit with half-even (banker's) rounding, once per
  movement. Floats are rejected. When stock is emptied the remaining value
  is absorbed in full, so a zero balance always carries zero value.
* An issue that cannot be covered by on-hand stock rejects the *whole*
  batch, even if the ending balance would be positive.
* Corrections replay only the affected product/location buckets. Untouched
  rows are carried over verbatim from the previous snapshot (the snapshot
  is never rebuilt from the full source), and every replaced report
  contribution is listed in the new snapshot's ``withdrawals`` table.
"""

from agate.inventory.config import BANKERS_ROUNDING, InventoryConfig, quantize_money
from agate.inventory.engine import Movement, extract_movements, replay_key, summarize_monthly_profit
from agate.inventory.exceptions import (InventoryAccountingError, InventoryInputError, InventorySequenceError,
                                        StockoutError)
from agate.inventory.ledger import InventoryLedger
from agate.inventory.snapshot import InventorySnapshot

__all__ = [
    'BANKERS_ROUNDING',
    'InventoryAccountingError',
    'InventoryConfig',
    'InventoryInputError',
    'InventoryLedger',
    'InventorySequenceError',
    'InventorySnapshot',
    'Movement',
    'StockoutError',
    'extract_movements',
    'quantize_money',
    'replay_key',
    'summarize_monthly_profit',
]
