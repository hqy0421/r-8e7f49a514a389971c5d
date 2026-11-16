"""
The correctable perpetual moving-average inventory ledger.

:class:`InventoryLedger` wraps an immutable agate movement table and keeps the
accounting state behind a sequence of versioned
:class:`~agate.inventory.snapshot.InventorySnapshot` objects. Corrections
(inserting a late goods receipt, changing an acquisition amount, reassigning a
movement to another product or location, deleting a wrong movement) are applied
in batches:

1. the corrected movement state is built in a throwaway copy;
2. every affected product/location is replayed and fully validated -- a
   mid-stream stockout, ambiguous ordering or any other defect aborts the
   whole batch before state changes (an all-or-nothing reject);
3. only the affected product/location buckets are recomputed. Every other row
   of the previous snapshot is carried over verbatim, keeping its original
   :class:`agate.Row` identity and generation; the snapshot is never rebuilt
   from the full source;
4. every old report contribution touched by the correction is recorded in the
   new snapshot's ``withdrawals`` table, pointing at its replacement snapshot.
"""

import json
from datetime import date as date_cls
from decimal import Decimal

from agate.data_types import Date, Text
from agate.inventory.config import ZERO, InventoryConfig, as_decimal, quantize_money
from agate.inventory.engine import Movement, extract_movements, replay_key, summarize_monthly_profit
from agate.inventory.exceptions import InventoryInputError, InventorySequenceError
from agate.inventory.snapshot import (BALANCE_NAMES, COST_DETAIL_NAMES, MONTHLY_PROFIT_NAMES, WITHDRAWAL_NAMES,
                                      InventorySnapshot, _as_rows, _merge_balances, _merge_detail, _merge_profit)

CORRECTION_COLUMNS = [
    ('correction_id', Text()),
    ('applied_at', Date()),
    ('operation', Text()),
    ('movement_id', Text()),
    ('description', Text()),
]
CORRECTION_NAMES = tuple(name for name, data_type in CORRECTION_COLUMNS)
CORRECTION_TYPES = tuple(data_type for name, data_type in CORRECTION_COLUMNS)


def _json_default(value):
    """
    JSON encoder fallback that keeps Decimals exact (as their canonical
    string) and renders dates as ISO strings, without touching float.
    """
    if isinstance(value, Decimal):
        return format(value, 'f')

    if isinstance(value, date_cls):
        return value.isoformat()

    return str(value)


class InventoryLedger:
    """
    A perpetual moving-average inventory ledger over an agate movement table.

    :param movements:
        An :class:`agate.Table` of goods movements. Number columns must be
        agate :class:`~agate.data_types.Number` (native
        :class:`decimal.Decimal`), the date column must be
        :class:`~agate.data_types.Date` or :class:`~agate.data_types.DateTime`.
        The source table itself is never modified or copied destructively.
    :param config:
        An :class:`InventoryConfig` describing column names, direction
        markers and currency precision. Defaults fit the documented schema.
    :param as_of:
        Publication date of the initial snapshot. Defaults to the current
        local date. Inject a fixed date in tests for reproducibility.
    """
    def __init__(self, movements, config=None, as_of=None):
        self._config = config or InventoryConfig()
        self._source_table = movements
        self._as_of = as_of or date_cls.today()

        movements_by_key, movements_by_id = extract_movements(movements, self._config)
        self._movements_by_key = movements_by_key
        self._movements_by_id = movements_by_id

        self._snapshots = []
        self._correction_records = []

        self._publish_initial()

    # ------------------------------------------------------------------ #
    # Construction helpers
    # ------------------------------------------------------------------ #
    def _publish_initial(self):
        detail_tuples = []
        balance_tuples = []

        for key in sorted(self._movements_by_key):
            detail_rows, balance_row = replay_key(
                self._movements_by_key[key], 1, self._config
            )
            detail_tuples.extend(detail_rows)
            balance_tuples.append(balance_row)

        profit_tuples = summarize_monthly_profit(detail_tuples, 1)

        snapshot = InventorySnapshot(
            self,
            generation=1,
            detail_rows=_as_rows(detail_tuples, COST_DETAIL_NAMES),
            balance_rows=_as_rows(balance_tuples, BALANCE_NAMES),
            monthly_profit_rows=_as_rows(profit_tuples, MONTHLY_PROFIT_NAMES),
            withdrawal_rows=[],
            created_at=self._as_of,
        )

        self._snapshots.append(snapshot)

    # ------------------------------------------------------------------ #
    # Read-side public API
    # ------------------------------------------------------------------ #
    @property
    def config(self):
        """The :class:`InventoryConfig` of this ledger."""
        return self._config

    @property
    def source_table(self):
        """The original, unmodified movement :class:`agate.Table`."""
        return self._source_table

    @property
    def snapshots(self):
        """Tuple of every published snapshot, oldest first."""
        return tuple(self._snapshots)

    @property
    def latest_snapshot(self):
        """The most recently published :class:`InventorySnapshot`."""
        return self._snapshots[-1]

    @property
    def corrections(self):
        """Tuple of applied correction records, in application order."""
        return tuple(self._correction_records)

    def snapshot(self, generation):
        """
        Return the snapshot published as ``generation`` (1-based).

        Old snapshots stay intact forever; corrections add a new snapshot
        and withdraw contributions in its audit table instead of mutating
        history.
        """
        return self._snapshots[generation - 1]

    # ------------------------------------------------------------------ #
    # Corrections
    # ------------------------------------------------------------------ #
    def insert_movement(self, values, as_of=None):
        """
        Apply a single ``insert`` correction. See :meth:`apply_corrections`.
        """
        return self.apply_corrections([{'op': 'insert', 'values': values}], as_of=as_of)

    def update_movement(self, movement_id, fields, as_of=None):
        """
        Apply a single ``update`` correction. See :meth:`apply_corrections`.
        """
        return self.apply_corrections(
            [{'op': 'update', 'id': movement_id, 'fields': fields}],
            as_of=as_of,
        )

    def delete_movement(self, movement_id, as_of=None):
        """
        Apply a single ``delete`` correction. See :meth:`apply_corrections`.
        """
        return self.apply_corrections([{'op': 'delete', 'id': movement_id}], as_of=as_of)

    def apply_corrections(self, corrections, as_of=None):
        """
        Apply a batch of corrections atomically and publish a new snapshot.

        Each correction is a mapping:

        * ``{'op': 'insert', 'values': {...}}`` -- add a movement; ``values``
          uses the configured column names.
        * ``{'op': 'update', 'id': ..., 'fields': {...}}`` -- change one or
          more columns of an existing movement.
        * ``{'op': 'delete', 'id': ...}`` -- remove a movement.

        All corrections are validated together against a throwaway copy of
        the ledger state. If any affected product/location cannot be replayed
        (for example an issue would stock out mid-stream), nothing is
        changed and the error propagates; the previous snapshot remains the
        latest one.

        :returns:
            The freshly published :class:`InventorySnapshot`.
        """
        if not corrections:
            raise InventoryInputError('No corrections were supplied.')

        applied_at = as_of or date_cls.today()
        parent = self.latest_snapshot
        new_generation = parent.generation + 1

        trial_by_id = dict(self._movements_by_id)
        directly_affected_keys = set()
        affected_products = set()
        normalized = []

        for correction in corrections:
            op = correction.get('op')

            if op == 'insert':
                movement = self._movement_from_mapping(correction.get('values') or {})

                if movement.id in trial_by_id:
                    raise InventoryInputError(
                        'Cannot insert movement "%s": the id already exists.' % movement.id
                    )

                trial_by_id[movement.id] = movement
                directly_affected_keys.add(movement.key)
                affected_products.add(movement.product)
                normalized.append(('insert', movement.id, movement))

            elif op == 'update':
                movement_id = correction.get('id')
                existing = trial_by_id.get(movement_id)

                if existing is None:
                    raise InventoryInputError(
                        'Cannot update movement "%s": no such movement id.' % movement_id
                    )

                fields = correction.get('fields') or {}
                unknown = set(fields) - set(self._config.required_columns())

                if unknown:
                    raise InventoryInputError(
                        'Unknown column(s) %s in update of movement "%s".'
                        % (sorted(unknown), movement_id)
                    )

                if self._config.id_column in fields:
                    raise InventoryInputError(
                        'Movement ids are stable and cannot be changed; delete and insert instead.'
                    )

                directly_affected_keys.add(existing.key)
                affected_products.add(existing.product)

                values = self._movement_to_mapping(existing)
                values.update(fields)
                updated = self._movement_from_mapping(values)

                trial_by_id[movement_id] = updated
                directly_affected_keys.add(updated.key)
                affected_products.add(updated.product)
                normalized.append(('update', movement_id, fields))

            elif op == 'delete':
                movement_id = correction.get('id')
                existing = trial_by_id.get(movement_id)

                if existing is None:
                    raise InventoryInputError(
                        'Cannot delete movement "%s": no such movement id.' % movement_id
                    )

                del trial_by_id[movement_id]
                directly_affected_keys.add(existing.key)
                affected_products.add(existing.product)
                normalized.append(('delete', movement_id, None))

            else:
                raise InventoryInputError(
                    'Correction %r has no valid "op" (insert, update or delete).' % correction
                )

        # Rebuild the key grouping from the trial state.
        trial_by_key = {}

        for movement in trial_by_id.values():
            trial_by_key.setdefault(movement.key, []).append(movement)

        for movements in trial_by_key.values():
            movements.sort(key=lambda m: m.order_key)

        # Structural validation only for keys that the batch can influence.
        for key in directly_affected_keys:
            movements = trial_by_key.get(key, ())
            positions = [m.order_key for m in movements]

            if len(set(positions)) != len(positions):
                raise InventorySequenceError(
                    'Product "%s" at location "%s" would have two movements with the same (date, sequence) '
                    'position after this correction.' % key
                )

            currencies = {m.currency for m in movements}

            if len(currencies) > 1:
                raise InventoryInputError(
                    'Product "%s" at location "%s" would mix currencies %s after this correction.'
                    % (key[0], key[1], sorted(currencies))
                )

        # Profit is reported per product, so every location of an affected
        # product must be replayed (even if only one location changed).
        replay_keys = sorted(
            key for key in trial_by_key
            if key[0] in affected_products
        )

        replay_output = {}

        # All-or-nothing: any failure here raises before state is mutated.
        for key in replay_keys:
            replay_output[key] = replay_key(trial_by_key[key], new_generation, self._config)

        # ----- the batch is valid; commit below this line --------------- #
        self._movements_by_id = trial_by_id
        self._movements_by_key = trial_by_key

        new_detail_tuples = []
        new_balance_tuples = []
        all_replayed_detail_tuples = []

        for key in replay_keys:
            detail_tuples, balance_tuple = replay_output[key]
            all_replayed_detail_tuples.extend(detail_tuples)

            if key in directly_affected_keys:
                new_detail_tuples.extend(detail_tuples)

                if balance_tuple is not None:
                    new_balance_tuples.append(balance_tuple)

        new_profit_tuples = summarize_monthly_profit(
            all_replayed_detail_tuples, new_generation
        )

        withdrawal_rows = self._build_withdrawals(
            parent, directly_affected_keys, affected_products, new_generation
        )

        detail_rows = _merge_detail(
            parent.cost_detail.rows, new_detail_tuples, directly_affected_keys
        )
        balance_rows = _merge_balances(
            parent.balances.rows, new_balance_tuples, directly_affected_keys
        )
        profit_rows = _merge_profit(
            parent.monthly_profit.rows, new_profit_tuples, affected_products
        )

        # Withdrawals accumulate: the full replacement trail stays visible.
        withdrawal_rows = list(parent.withdrawals.rows) + withdrawal_rows

        snapshot = InventorySnapshot(
            self,
            generation=new_generation,
            detail_rows=detail_rows,
            balance_rows=balance_rows,
            monthly_profit_rows=profit_rows,
            withdrawal_rows=withdrawal_rows,
            created_at=applied_at,
        )

        self._snapshots.append(snapshot)
        self._record_corrections(normalized, applied_at)

        return snapshot

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #
    def _build_withdrawals(self, parent, affected_keys, affected_products, new_generation):
        old_generation = Decimal(parent.generation)
        new_generation_decimal = Decimal(new_generation)
        rows = []

        for row in parent.cost_detail.rows:
            if (row['product'], row['location']) in affected_keys:
                rows.append((
                    'cost_detail',
                    row['product'], row['location'], row['currency'],
                    row['business_date'], None, None, row['movement_id'],
                    old_generation, new_generation_decimal, new_generation_decimal,
                ))

        for row in parent.balances.rows:
            if (row['product'], row['location']) in affected_keys:
                rows.append((
                    'balances',
                    row['product'], row['location'], row['currency'],
                    None, None, None, None,
                    old_generation, new_generation_decimal, new_generation_decimal,
                ))

        for row in parent.monthly_profit.rows:
            if row['product'] in affected_products:
                rows.append((
                    'monthly_profit',
                    row['product'], None, row['currency'],
                    None, row['year'], row['month'], None,
                    old_generation, new_generation_decimal, new_generation_decimal,
                ))

        return _as_rows(rows, WITHDRAWAL_NAMES)

    def _record_corrections(self, normalized, applied_at):
        from agate.rows import Row
        from agate.table import Table

        start = len(self._correction_records)
        new_rows = []

        for index, (operation, movement_id, payload) in enumerate(normalized):
            correction_id = 'C%i' % (start + index + 1)

            if operation == 'insert' and payload is not None:
                payload = self._movement_to_mapping(payload)

            description = json.dumps(payload, default=_json_default, sort_keys=True)

            record = {
                'correction_id': correction_id,
                'applied_at': applied_at,
                'operation': operation,
                'movement_id': movement_id,
                'description': description,
            }
            self._correction_records.append(record)
            new_rows.append(Row(tuple(record[name] for name in CORRECTION_NAMES), CORRECTION_NAMES))

        if not hasattr(self, '_correction_table'):
            self._correction_table = Table(
                new_rows, CORRECTION_NAMES, CORRECTION_TYPES, _is_fork=True
            )
        else:
            merged = list(self._correction_table.rows) + new_rows
            self._correction_table = Table(
                merged, CORRECTION_NAMES, CORRECTION_TYPES, _is_fork=True
            )

    @property
    def correction_table(self):
        """
        An agate :class:`agate.Table` audit trail of every applied correction.
        """
        return self._correction_table

    def _movement_to_mapping(self, movement):
        config = self._config

        values = {
            config.id_column: movement.id,
            config.product_column: movement.product,
            config.location_column: movement.location,
            config.date_column: movement.business_date,
            config.sequence_column: Decimal(movement.sequence),
            config.direction_column: movement.direction,
            config.quantity_column: movement.quantity,
            config.amount_column: movement.amount,
        }

        if config.currency_column:
            values[config.currency_column] = movement.currency

        return values

    def _movement_from_mapping(self, values):
        config = self._config

        if not isinstance(values, dict):
            raise InventoryInputError(
                'Movement payloads must be mappings of column name to native value, got %s.'
                % type(values).__name__
            )

        missing = [name for name in config.required_columns() if name not in values]

        if missing:
            raise InventoryInputError('Movement payload is missing column(s): %s.' % ', '.join(sorted(missing)))

        movement_id = values[config.id_column]

        if not isinstance(movement_id, str):
            raise InventoryInputError('Movement id must be text, got %r.' % (movement_id,))

        product = values[config.product_column]
        location = values[config.location_column]

        if not isinstance(product, str) or not isinstance(location, str):
            raise InventoryInputError('Product and location must be text values.')

        raw_date = values[config.date_column]

        if isinstance(raw_date, date_cls):
            business_date = raw_date
        else:
            raise InventoryInputError(
                'Business date for movement "%s" must be a native datetime.date, got %r.'
                % (movement_id, raw_date)
            )

        sequence = as_decimal(values[config.sequence_column], config.sequence_column)

        if sequence != sequence.to_integral_value():
            raise InventoryInputError(
                'Sequence for movement "%s" must be an integer, got %s.' % (movement_id, sequence)
            )

        direction = values[config.direction_column]

        if direction not in (config.receipt_marker, config.issue_marker):
            raise InventoryInputError(
                'Movement "%s" has unknown direction %r.' % (movement_id, direction)
            )

        quantity = as_decimal(values[config.quantity_column], config.quantity_column)

        if quantity <= ZERO:
            raise InventoryInputError(
                'Movement "%s" must carry a positive quantity, got %s.' % (movement_id, quantity)
            )

        if config.currency_column:
            currency = values[config.currency_column]

            if not isinstance(currency, str):
                raise InventoryInputError('Currency must be text, got %r.' % (currency,))
        else:
            currency = config.default_currency

        minor_unit = config.minor_unit(currency)
        amount = as_decimal(values[config.amount_column], config.amount_column, nullable=True)

        if amount is None:
            if direction == config.receipt_marker:
                raise InventoryInputError(
                    'Receipt movement "%s" requires an acquisition amount.' % movement_id
                )
        else:
            amount = quantize_money(amount, minor_unit)

        return Movement(
            movement_id, product, location, business_date, int(sequence),
            direction, quantity, amount, currency,
        )
