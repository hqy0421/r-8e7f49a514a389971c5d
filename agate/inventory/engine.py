"""
The perpetual moving-average cost engine.

The engine is deliberately functional and side-effect free:

* :func:`extract_movements` validates an agate movement table and groups the
  normalized :class:`Movement` records by ``(product, location)``.
* :func:`replay_key` processes one product/location in business order and
  returns cost-detail rows plus the ending balance.
* :func:`summarize_monthly_profit` aggregates issue rows into the per-product
  monthly gross-profit table.

All arithmetic is :class:`decimal.Decimal`; monetary values are quantized to
the currency's smallest unit with half-even rounding exactly once per
movement, while average unit costs keep full precision.
"""

from datetime import date, datetime
from decimal import Decimal

from agate.inventory.config import ZERO, as_decimal, quantize_money
from agate.inventory.exceptions import InventoryInputError, InventorySequenceError, StockoutError


class Movement:
    """
    One normalized goods movement.

    Attributes are native Python values: strings for text columns,
    :class:`datetime.date` for the business date, :class:`int` for the
    sequence, :class:`decimal.Decimal` for quantity and amount.
    """
    __slots__ = (
        'id', 'product', 'location', 'business_date', 'sequence',
        'direction', 'quantity', 'amount', 'currency',
    )

    def __init__(self, id, product, location, business_date, sequence, direction, quantity, amount, currency):
        self.id = id
        self.product = product
        self.location = location
        self.business_date = business_date
        self.sequence = sequence
        self.direction = direction
        self.quantity = quantity
        self.amount = amount
        self.currency = currency

    @property
    def key(self):
        return self.product, self.location

    @property
    def order_key(self):
        return self.business_date, self.sequence


def _check_column_types(table, config):
    expected = config.expected_input_types()

    for column_name, expected_type in expected.items():
        try:
            column = table.columns[column_name]
        except KeyError:
            raise InventoryInputError(
                'Movement table is missing required column "%s".' % column_name
            )

        if not isinstance(column.data_type, expected_type):
            raise InventoryInputError(
                'Column "%s" must use agate data type %s (got %s), so that values keep their native '
                'Python/Decimal type.' % (
                    column_name,
                    expected_type if isinstance(expected_type, tuple) else expected_type.__name__,
                    type(column.data_type).__name__,
                )
            )


def _require_text(row, column_name):
    value = row[column_name]

    if value is None:
        raise InventoryInputError('Column "%s" contains a null identifier.' % column_name)

    if not isinstance(value, str):
        raise InventoryInputError('Column "%s" must contain text, got %r.' % (column_name, value))

    return value


def extract_movements(table, config):
    """
    Validate a movement :class:`agate.Table` and group its rows by key.

    :returns:
        A tuple ``(movements_by_key, movements_by_id)`` where the first maps
        ``(product, location)`` to a chronologically ordered list of
        :class:`Movement` records and the second maps movement id to the
        same record.
    """
    _check_column_types(table, config)

    movements_by_id = {}
    movements_by_key = {}

    for row in table.rows:
        movement_id = _require_text(row, config.id_column)

        if movement_id in movements_by_id:
            raise InventoryInputError('Duplicate movement id "%s".' % movement_id)

        product = _require_text(row, config.product_column)
        location = _require_text(row, config.location_column)

        raw_date = row[config.date_column]

        if isinstance(raw_date, datetime):
            business_date = raw_date.date()
        elif isinstance(raw_date, date):
            business_date = raw_date
        else:
            raise InventoryInputError(
                'Column "%s" must contain native date/datetime values, got %r.'
                % (config.date_column, raw_date)
            )

        sequence_decimal = as_decimal(row[config.sequence_column], config.sequence_column)

        if sequence_decimal != sequence_decimal.to_integral_value():
            raise InventoryInputError(
                'Sequence for movement "%s" must be an integer, got %s.' % (movement_id, sequence_decimal)
            )

        sequence = int(sequence_decimal)

        direction = row[config.direction_column]

        if direction not in (config.receipt_marker, config.issue_marker):
            raise InventoryInputError(
                'Movement "%s" has unknown direction %r; expected %r or %r.'
                % (movement_id, direction, config.receipt_marker, config.issue_marker)
            )

        quantity = as_decimal(row[config.quantity_column], config.quantity_column)

        if quantity <= ZERO:
            raise InventoryInputError(
                'Movement "%s" must carry a positive quantity, got %s.' % (movement_id, quantity)
            )

        if config.currency_column:
            currency = _require_text(row, config.currency_column)
        else:
            currency = config.default_currency

        minor_unit = config.minor_unit(currency)

        raw_amount = row[config.amount_column]
        amount = as_decimal(raw_amount, config.amount_column, nullable=True)

        if amount is None:
            if direction == config.receipt_marker:
                raise InventoryInputError(
                    'Receipt movement "%s" requires an acquisition amount.' % movement_id
                )
        else:
            # Quantize once, on ingest, with half-even rounding.
            amount = quantize_money(amount, minor_unit)

        movement = Movement(
            movement_id, product, location, business_date, sequence,
            direction, quantity, amount, currency,
        )

        movements_by_id[movement_id] = movement
        movements_by_key.setdefault(movement.key, []).append(movement)

    for key, movements in movements_by_key.items():
        movements.sort(key=lambda m: m.order_key)

        positions = [m.order_key for m in movements]

        if len(set(positions)) != len(positions):
            raise InventorySequenceError(
                'Product "%s" at location "%s" has two movements with the same (date, sequence) position; '
                'their processing order is ambiguous.' % key
            )

        currencies = {m.currency for m in movements}

        if len(currencies) > 1:
            raise InventoryInputError(
                'Product "%s" at location "%s" mixes currencies %s; a moving-average pool cannot average '
                'different currencies together.' % (key[0], key[1], sorted(currencies))
            )

    return movements_by_key, movements_by_id


def replay_key(movements, generation, config):
    """
    Run perpetual moving-average accounting for one product/location.

    :param movements:
        The chronologically ordered :class:`Movement` records of one
        ``(product, location)`` key.
    :param generation:
        Snapshot generation stamped onto every produced row, so carried and
        recomputed rows remain distinguishable.
    :returns:
        A tuple ``(detail_rows, balance_row)`` where ``detail_rows`` is a
        list of plain tuples matching the cost-detail schema and
        ``balance_row`` is the ending-balance tuple (``None`` when the key
        no longer has any movements, i.e. after a deletion-only correction).
    :raises StockoutError:
        If an issue requests more units than are on hand. The caller
        rejects the whole batch; no partial state is kept.
    """
    detail_rows = []

    on_hand_quantity = ZERO
    on_hand_value = ZERO

    for movement in movements:
        minor_unit = config.minor_unit(movement.currency)

        if movement.direction == config.receipt_marker:
            receipt_amount = quantize_money(movement.amount, minor_unit)
            cogs = None
            revenue = None
            gross_margin = None
            unit_cost_used = None

            on_hand_quantity += movement.quantity
            on_hand_value += receipt_amount
        else:
            if movement.quantity > on_hand_quantity:
                raise StockoutError(
                    movement.id, movement.product, movement.location,
                    movement.business_date, on_hand_quantity, movement.quantity,
                )

            receipt_amount = None

            if movement.quantity == on_hand_quantity:
                # The stock is emptied: the residual value is absorbed in
                # full so that no rounding dust can remain on a zero stock.
                cogs = on_hand_value
            else:
                average_unit_cost = on_hand_value / on_hand_quantity
                cogs = quantize_money(average_unit_cost * movement.quantity, minor_unit)

            on_hand_quantity -= movement.quantity
            on_hand_value -= cogs

            if movement.amount is None:
                revenue = None
                gross_margin = None
            else:
                revenue = quantize_money(movement.amount, minor_unit)
                gross_margin = revenue - cogs

            unit_cost_used = cogs / movement.quantity

        if on_hand_quantity == ZERO:
            # Defensive counterpart of the emptying branch above.
            on_hand_value = ZERO
            on_hand_unit_cost = ZERO
        else:
            on_hand_unit_cost = on_hand_value / on_hand_quantity

        if movement.direction == config.receipt_marker:
            unit_cost = on_hand_unit_cost
        else:
            unit_cost = unit_cost_used

        detail_rows.append((
            movement.id,
            movement.product,
            movement.location,
            movement.currency,
            movement.business_date,
            Decimal(movement.sequence),
            movement.direction,
            movement.quantity,
            unit_cost,
            receipt_amount,
            cogs,
            revenue,
            gross_margin,
            on_hand_quantity,
            on_hand_value,
            on_hand_unit_cost,
            Decimal(generation),
        ))

    balance_row = None

    if movements:
        last = movements[-1]

        if on_hand_quantity == ZERO:
            on_hand_unit_cost = ZERO
        else:
            on_hand_unit_cost = on_hand_value / on_hand_quantity

        balance_row = (
            last.product,
            last.location,
            last.currency,
            on_hand_quantity,
            on_hand_value,
            on_hand_unit_cost,
            Decimal(generation),
        )

    return detail_rows, balance_row


def summarize_monthly_profit(detail_rows, generation):
    """
    Aggregate cost-detail rows into per-product monthly gross profit.

    Issues are grouped by ``(product, currency, year, month)``. Receipts do
    not contribute. A bucket whose issues all have null revenue yields null
    revenue and gross margin while still reporting issued quantity and cost
    of goods sold.
    """
    buckets = {}

    for row in detail_rows:
        (movement_id, product, location, currency, business_date, sequence,
         direction, quantity, unit_cost, receipt_amount, cogs, revenue,
         gross_margin, on_hand_quantity, on_hand_value, on_hand_unit_cost,
         row_generation) = row

        if cogs is None:
            continue

        bucket_key = (product, currency, business_date.year, business_date.month)
        bucket = buckets.get(bucket_key)

        if bucket is None:
            bucket = {
                'issue_quantity': ZERO,
                'revenue': ZERO,
                'cogs': ZERO,
                'revenue_null': True,
            }
            buckets[bucket_key] = bucket

        bucket['issue_quantity'] += quantity
        bucket['cogs'] += cogs

        if revenue is not None:
            bucket['revenue_null'] = False
            bucket['revenue'] += revenue

    summary_rows = []

    for bucket_key in sorted(buckets):
        product, currency, year, month = bucket_key
        bucket = buckets[bucket_key]

        if bucket['revenue_null']:
            revenue = None
            gross_margin = None
        else:
            revenue = bucket['revenue']
            gross_margin = bucket['revenue'] - bucket['cogs']

        summary_rows.append((
            product,
            currency,
            Decimal(year),
            Decimal(month),
            bucket['issue_quantity'],
            revenue,
            bucket['cogs'],
            gross_margin,
            Decimal(generation),
        ))

    return summary_rows
