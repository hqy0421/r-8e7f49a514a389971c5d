"""
The immutable reporting snapshot produced by an inventory ledger.

A :class:`InventorySnapshot` is a read-only bundle of four agate tables:

* :attr:`cost_detail` -- one row per movement with unit cost, receipt amount
  or issue cost/revenue/margin and the running on-hand quantity, value and
  average unit cost.
* :attr:`balances` -- one ending-balance row per product/location.
* :attr:`monthly_profit` -- issued quantity, revenue, cost of goods sold and
  gross margin per product, currency and calendar month.
* :attr:`withdrawals` -- the rows that this snapshot replaced in the previous
  one ("撤换旧报表贡献"), with provenance back to the original movement.

Snapshots are never recomputed from the full source after a correction.
Unchanged buckets are carried verbatim -- the exact same :class:`agate.Row`
instances, still marked with the generation at which they were first
computed -- and only affected product/location buckets are replayed.
"""

from agate.data_types import Date, Number, Text
from agate.rows import Row
from agate.table import Table

#: Column names and native agate types of the cost-detail table.
COST_DETAIL_COLUMNS = [
    ('movement_id', Text()),
    ('product', Text()),
    ('location', Text()),
    ('currency', Text()),
    ('business_date', Date()),
    ('sequence', Number()),
    ('direction', Text()),
    ('quantity', Number()),
    ('unit_cost', Number()),
    ('receipt_amount', Number()),
    ('cogs', Number()),
    ('revenue', Number()),
    ('gross_margin', Number()),
    ('on_hand_quantity', Number()),
    ('on_hand_value', Number()),
    ('on_hand_unit_cost', Number()),
    ('generation', Number()),
]

#: Column names and native agate types of the ending-balance table.
BALANCE_COLUMNS = [
    ('product', Text()),
    ('location', Text()),
    ('currency', Text()),
    ('on_hand_quantity', Number()),
    ('on_hand_value', Number()),
    ('on_hand_unit_cost', Number()),
    ('generation', Number()),
]

#: Column names and native agate types of the monthly gross-profit table.
MONTHLY_PROFIT_COLUMNS = [
    ('product', Text()),
    ('currency', Text()),
    ('year', Number()),
    ('month', Number()),
    ('issue_quantity', Number()),
    ('revenue', Number()),
    ('cogs', Number()),
    ('gross_margin', Number()),
    ('generation', Number()),
]

#: Column names and native agate types of the withdrawal ledger.
WITHDRAWAL_COLUMNS = [
    ('report', Text()),
    ('product', Text()),
    ('location', Text()),
    ('currency', Text()),
    ('business_date', Date()),
    ('year', Number()),
    ('month', Number()),
    ('movement_id', Text()),
    ('withdrawn_snapshot', Number()),
    ('replacement_snapshot', Number()),
    ('generation', Number()),
]

COST_DETAIL_NAMES = tuple(name for name, data_type in COST_DETAIL_COLUMNS)
BALANCE_NAMES = tuple(name for name, data_type in BALANCE_COLUMNS)
MONTHLY_PROFIT_NAMES = tuple(name for name, data_type in MONTHLY_PROFIT_COLUMNS)
WITHDRAWAL_NAMES = tuple(name for name, data_type in WITHDRAWAL_COLUMNS)

COST_DETAIL_TYPES = tuple(data_type for name, data_type in COST_DETAIL_COLUMNS)
BALANCE_TYPES = tuple(data_type for name, data_type in BALANCE_COLUMNS)
MONTHLY_PROFIT_TYPES = tuple(data_type for name, data_type in MONTHLY_PROFIT_COLUMNS)
WITHDRAWAL_TYPES = tuple(data_type for name, data_type in WITHDRAWAL_COLUMNS)

DETAIL_SORT = (1, 2, 4, 5)      # product, location, business_date, sequence
BALANCE_SORT = (0, 1)           # product, location
PROFIT_SORT = (0, 1, 2, 3)      # product, currency, year, month
WITHDRAWAL_SORT = (0, 1, 2, 4, 7)


def _as_rows(tuples, names):
    return [Row(values, names) for values in tuples]


def _build_table(rows, names, types, sort_indexes):
    rows = list(rows)

    if sort_indexes:
        rows.sort(key=lambda row: tuple(row[i] for i in sort_indexes))

    # _is_fork=True: the values are already native and Row instances keep
    # their identity, so carried rows are literally shared with the parent.
    return Table(rows, names, types, _is_fork=True)


def _merge_detail(parent_rows, new_tuples, affected_keys):
    carried = [
        row for row in parent_rows
        if (row['product'], row['location']) not in affected_keys
    ]
    carried.extend(_as_rows(new_tuples, COST_DETAIL_NAMES))
    return carried


def _merge_balances(parent_rows, new_balance_tuples, affected_keys):
    # Balances of fully deleted buckets disappear; the replay returns no
    # balance row for them, so simply dropping every parent row of an
    # affected key and adding the fresh ones is correct.
    carried = [
        row for row in parent_rows
        if (row['product'], row['location']) not in affected_keys
    ]
    carried.extend(_as_rows(new_balance_tuples, BALANCE_NAMES))
    return carried


def _merge_profit(parent_rows, new_profit_tuples, affected_products):
    carried = [row for row in parent_rows if row['product'] not in affected_products]
    carried.extend(_as_rows(new_profit_tuples, MONTHLY_PROFIT_NAMES))
    return carried


class InventorySnapshot:
    """
    An immutable, versioned bundle of inventory accounting tables.

    Instances are created by :class:`~agate.inventory.InventoryLedger`; do
    not construct them directly. All four attributes are ordinary
    :class:`agate.Table` objects with real native column types
    (:class:`~agate.data_types.Number` columns hold
    :class:`decimal.Decimal`, the date column holds :class:`datetime.date`).
    """
    def __init__(self, ledger, generation, detail_rows, balance_rows,
                 monthly_profit_rows, withdrawal_rows, created_at):
        self._ledger = ledger
        self._generation = generation
        self._created_at = created_at

        self._cost_detail = _build_table(
            detail_rows, COST_DETAIL_NAMES, COST_DETAIL_TYPES, DETAIL_SORT
        )
        self._balances = _build_table(
            balance_rows, BALANCE_NAMES, BALANCE_TYPES, BALANCE_SORT
        )
        self._monthly_profit = _build_table(
            monthly_profit_rows, MONTHLY_PROFIT_NAMES, MONTHLY_PROFIT_TYPES, PROFIT_SORT
        )
        self._withdrawals = _build_table(
            withdrawal_rows, WITHDRAWAL_NAMES, WITHDRAWAL_TYPES, WITHDRAWAL_SORT
        )

    @property
    def ledger(self):
        """The :class:`~agate.inventory.InventoryLedger` that published this snapshot."""
        return self._ledger

    @property
    def generation(self):
        """Monotonically increasing version number, starting at ``1``."""
        return self._generation

    @property
    def created_at(self):
        """The :class:`datetime.date` on which this snapshot was published."""
        return self._created_at

    @property
    def cost_detail(self):
        """Per-movement cost detail with running on-hand quantities and values."""
        return self._cost_detail

    @property
    def balances(self):
        """Ending quantity, value and average unit cost per product/location."""
        return self._balances

    @property
    def monthly_profit(self):
        """Issued quantity, revenue, COGS and gross margin per product/month."""
        return self._monthly_profit

    @property
    def withdrawals(self):
        """Contributions withdrawn (replaced) by this and earlier corrections."""
        return self._withdrawals
