=========
Inventory
=========

.. automodule:: agate.inventory
    :no-members:

The :mod:`agate.inventory` subpackage adds correctable perpetual
moving-average inventory accounting on top of an ordinary movement
:class:`.Table`. Goods receipts and issues are processed per product and
storage location in business order ``(date, sequence)``; the cost of each
issue is taken from the moving average of the quantity and value currently
on hand.

Every monetary value is a :class:`decimal.Decimal` quantized to the smallest
unit of the agreed currency with half-even (banker's) rounding, exactly once
per movement. :class:`float` is rejected, so binary floating point never
enters the calculation. When an issue empties the stock, the remaining value
is absorbed in full and the zero balance always carries zero value.

An issue that cannot be covered by the stock on hand rejects the *whole*
batch -- including a correction batch -- even when a later receipt would
leave a positive ending balance, because the issue cost would otherwise be
undefined.

Corrections (a late receipt, a changed acquisition amount, a different
product/location assignment, or a deletion) never rewrite history. They
replay only the affected product/location buckets, carry every untouched row
of the previous snapshot over verbatim (the snapshot is never rebuilt from
the full source), and list each replaced report contribution in the new
snapshot's :attr:`~agate.inventory.InventorySnapshot.withdrawals` table.

.. code-block:: python

    import agate
    from decimal import Decimal
    from datetime import date

    movements = agate.Table(rows, column_names, [
        agate.Text(), agate.Text(), agate.Text(), agate.Date(), agate.Number(),
        agate.Text(), agate.Number(), agate.Number(), agate.Text(),
    ])

    ledger = agate.InventoryLedger(movements)
    snapshot = ledger.latest_snapshot

    snapshot.cost_detail.print_table()
    snapshot.balances.print_table()
    snapshot.monthly_profit.print_table()

    corrected = ledger.insert_movement({
        'id': 'M99', 'product': 'WIDGET', 'location': 'A1',
        'business_date': date(2026, 1, 5), 'sequence': Decimal(5),
        'direction': 'receipt', 'quantity': Decimal(10),
        'amount': Decimal('120.00'), 'currency': 'USD',
    })

Configuration
-------------

.. autosummary::
    :nosignatures:

    agate.inventory.InventoryConfig

.. autoclass:: agate.inventory.InventoryConfig
    :members:

Ledger and snapshots
--------------------

.. autosummary::
    :nosignatures:

    agate.inventory.InventoryLedger
    agate.inventory.InventorySnapshot
    agate.inventory.Movement

.. autoclass:: agate.inventory.InventoryLedger
    :members:

.. autoclass:: agate.inventory.InventorySnapshot
    :members:

.. autoclass:: agate.inventory.Movement
    :members:

Numeric helpers
---------------

.. autosummary::
    :nosignatures:

    agate.inventory.quantize_money
    agate.inventory.BANKERS_ROUNDING

.. autofunction:: agate.inventory.quantize_money

.. autodata:: agate.inventory.BANKERS_ROUNDING

Low-level engine
----------------

These functions power the ledger and can be used directly when agate tables
are not needed.

.. autosummary::
    :nosignatures:

    agate.inventory.extract_movements
    agate.inventory.replay_key
    agate.inventory.summarize_monthly_profit

.. autofunction:: agate.inventory.extract_movements
.. autofunction:: agate.inventory.replay_key
.. autofunction:: agate.inventory.summarize_monthly_profit

Exceptions
----------

.. autosummary::
    :nosignatures:

    agate.inventory.InventoryAccountingError
    agate.inventory.InventoryInputError
    agate.inventory.InventorySequenceError
    agate.inventory.StockoutError

.. autoexception:: agate.inventory.InventoryAccountingError
.. autoexception:: agate.inventory.InventoryInputError
.. autoexception:: agate.inventory.InventorySequenceError
.. autoexception:: agate.inventory.StockoutError
