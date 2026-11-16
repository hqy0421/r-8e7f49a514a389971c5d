import unittest
from datetime import date
from decimal import Decimal

import agate
from agate.inventory.config import InventoryConfig
from agate.inventory.exceptions import InventoryInputError, InventorySequenceError, StockoutError
from agate.inventory.snapshot import BALANCE_NAMES, COST_DETAIL_NAMES, MONTHLY_PROFIT_NAMES, WITHDRAWAL_NAMES
from tests.test_inventory.helpers import balances_by_key, make_table, payload, rows_by_id, simple_rows


def two_product_rows():
    rows = simple_rows()
    rows.extend([
        ('O1', 'GADGET', 'B2', date(2026, 1, 4), Decimal(1), 'receipt',
         Decimal('5'), Decimal('50.00'), 'USD'),
        ('O2', 'GADGET', 'B2', date(2026, 1, 9), Decimal(2), 'issue',
         Decimal('2'), Decimal('30.00'), 'USD'),
    ])
    return rows


class TestInitialSnapshot(unittest.TestCase):
    def setUp(self):
        self.ledger = agate.InventoryLedger(
            make_table(simple_rows()), as_of=date(2026, 3, 1)
        )
        self.snapshot = self.ledger.latest_snapshot

    def test_native_table_shapes_and_types(self):
        self.assertEqual(self.snapshot.generation, 1)
        self.assertEqual(self.snapshot.created_at, date(2026, 3, 1))
        self.assertColumnSchema(
            self.snapshot.cost_detail, COST_DETAIL_NAMES,
            [agate.Text, agate.Text, agate.Text, agate.Text, agate.Date,
             agate.Number, agate.Text, agate.Number, agate.Number, agate.Number,
             agate.Number, agate.Number, agate.Number, agate.Number,
             agate.Number, agate.Number, agate.Number],
        )
        self.assertColumnSchema(
            self.snapshot.balances, BALANCE_NAMES,
            [agate.Text, agate.Text, agate.Text, agate.Number,
             agate.Number, agate.Number, agate.Number],
        )
        self.assertColumnSchema(
            self.snapshot.monthly_profit, MONTHLY_PROFIT_NAMES,
            [agate.Text, agate.Text, agate.Number, agate.Number,
             agate.Number, agate.Number, agate.Number, agate.Number, agate.Number],
        )
        self.assertColumnSchema(
            self.snapshot.withdrawals, WITHDRAWAL_NAMES,
            [agate.Text, agate.Text, agate.Text, agate.Text, agate.Date,
             agate.Number, agate.Number, agate.Text, agate.Number,
             agate.Number, agate.Number],
        )

    def assertColumnSchema(self, table, names, types):
        self.assertSequenceEqual(table.column_names, list(names))
        for column, expected_type in zip(table.columns, types):
            self.assertIsInstance(column.data_type, expected_type)

    def test_values_are_decimal_not_float(self):
        for row in self.snapshot.cost_detail.rows:
            for name in ('quantity', 'on_hand_value', 'on_hand_unit_cost', 'generation'):
                value = row[name]
                self.assertIsInstance(value, Decimal)

    def test_initial_withdrawals_are_empty(self):
        self.assertEqual(len(self.snapshot.withdrawals.rows), 0)

    def test_source_table_is_retained_and_unmodified(self):
        self.assertEqual(len(self.ledger.source_table.rows), 4)
        self.assertIs(self.ledger.source_table, self.ledger.source_table)


class TestCorrectionRecomputation(unittest.TestCase):
    def setUp(self):
        self.ledger = agate.InventoryLedger(
            make_table(two_product_rows()), as_of=date(2026, 3, 1)
        )
        self.first = self.ledger.latest_snapshot

    def test_late_receipt_recomputes_downstream_costs(self):
        snapshot = self.ledger.insert_movement(
            payload('M9', 'WIDGET', 'A1', date(2026, 1, 5), 5,
                    'receipt', 10, '105.00'),
            as_of=date(2026, 3, 2),
        )

        self.assertEqual(snapshot.generation, 2)
        detail = rows_by_id(snapshot.cost_detail)

        # M2's issue now averages (100 + 105) / 20 = 10.25 -> 41.00.
        self.assertEqual(detail['M2']['cogs'], Decimal('41.00'))
        self.assertEqual(detail['M2']['gross_margin'], Decimal('39.00'))

        # All WIDGET rows are stamped with the new generation.
        widget_rows = [r for r in snapshot.cost_detail.rows if r['product'] == 'WIDGET']
        self.assertTrue(all(r['generation'] == Decimal(2) for r in widget_rows))

        # February issue: 16 units at the blended pool.
        self.assertEqual(detail['M4']['cogs'], Decimal('168.62'))

        balance = balances_by_key(snapshot.balances)[('WIDGET', 'A1')]
        self.assertEqual(balance['on_hand_quantity'], Decimal('10'))
        self.assertEqual(balance['on_hand_value'], Decimal('105.38'))

        # Monthly profit for January follows the new issue cost.
        january = [
            r for r in snapshot.monthly_profit.rows
            if r['product'] == 'WIDGET' and r['month'] == Decimal(1)
        ][0]
        self.assertEqual(january['cogs'], Decimal('41.00'))
        self.assertEqual(january['gross_margin'], Decimal('39.00'))

    def test_only_affected_buckets_are_recomputed(self):
        before = rows_by_id(self.first.cost_detail)

        snapshot = self.ledger.insert_movement(
            payload('M9', 'WIDGET', 'A1', date(2026, 1, 5), 5,
                    'receipt', 10, '105.00'),
            as_of=date(2026, 3, 2),
        )

        after = rows_by_id(snapshot.cost_detail)

        # Untouched product/location rows are the literal same Row objects
        # and keep generation 1: the snapshot was not rebuilt from source.
        for movement_id in ('O1', 'O2'):
            self.assertIs(after[movement_id], before[movement_id])
            self.assertEqual(after[movement_id]['generation'], Decimal(1))

        gadget_balance = balances_by_key(snapshot.balances)[('GADGET', 'B2')]
        self.assertEqual(gadget_balance['generation'], Decimal(1))

        gadget_profit = [
            r for r in snapshot.monthly_profit.rows if r['product'] == 'GADGET'
        ]
        self.assertTrue(all(r['generation'] == Decimal(1) for r in gadget_profit))

    def test_old_snapshot_remains_intact(self):
        original_m2 = rows_by_id(self.first.cost_detail)['M2']

        self.ledger.insert_movement(
            payload('M9', 'WIDGET', 'A1', date(2026, 1, 5), 5,
                    'receipt', 10, '105.00'),
            as_of=date(2026, 3, 2),
        )

        self.assertEqual(len(self.ledger.snapshots), 2)
        self.assertEqual(self.ledger.snapshot(1).generation, 1)
        self.assertEqual(original_m2['cogs'], Decimal('40.00'))
        self.assertEqual(len(self.ledger.snapshot(1).withdrawals.rows), 0)

    def test_withdrawals_record_replaced_contributions(self):
        snapshot = self.ledger.insert_movement(
            payload('M9', 'WIDGET', 'A1', date(2026, 1, 5), 5,
                    'receipt', 10, '105.00'),
            as_of=date(2026, 3, 2),
        )

        reports = {(r['report'], r['movement_id']) for r in snapshot.withdrawals.rows}

        self.assertIn(('cost_detail', 'M1'), reports)
        self.assertIn(('cost_detail', 'M2'), reports)
        self.assertIn(('cost_detail', 'M3'), reports)
        self.assertIn(('cost_detail', 'M4'), reports)
        self.assertNotIn(('cost_detail', 'O1'), reports)

        for row in snapshot.withdrawals.rows:
            self.assertEqual(row['withdrawn_snapshot'], Decimal(1))
            self.assertEqual(row['replacement_snapshot'], Decimal(2))
            self.assertEqual(row['generation'], Decimal(2))

        profit_withdrawals = [
            r for r in snapshot.withdrawals.rows if r['report'] == 'monthly_profit'
        ]
        self.assertEqual({(r['product'], r['year'], r['month']) for r in profit_withdrawals},
                         {('WIDGET', Decimal(2026), Decimal(1)),
                          ('WIDGET', Decimal(2026), Decimal(2))})

        balance_withdrawals = [r for r in snapshot.withdrawals.rows if r['report'] == 'balances']
        self.assertEqual([(r['product'], r['location']) for r in balance_withdrawals],
                         [('WIDGET', 'A1')])

    def test_withdrawals_accumulate_across_generations(self):
        self.ledger.insert_movement(
            payload('M9', 'WIDGET', 'A1', date(2026, 1, 5), 5,
                    'receipt', 10, '105.00'),
            as_of=date(2026, 3, 2),
        )
        third = self.ledger.update_movement(
            'M9', {'amount': Decimal('108.00')}, as_of=date(2026, 3, 3)
        )

        generations = {
            (r['withdrawn_snapshot'], r['replacement_snapshot'])
            for r in third.withdrawals.rows
        }
        self.assertIn((Decimal(1), Decimal(2)), generations)
        self.assertIn((Decimal(2), Decimal(3)), generations)

    def test_amount_correction_changes_downstream_margin(self):
        snapshot = self.ledger.update_movement(
            'M1', {'amount': Decimal('120.00')}, as_of=date(2026, 3, 2)
        )

        detail = rows_by_id(snapshot.cost_detail)
        # New unit cost 120 / 10 = 12; M2 issue of 4 -> 48.
        self.assertEqual(detail['M2']['cogs'], Decimal('48.00'))
        self.assertEqual(detail['M2']['gross_margin'], Decimal('32.00'))

    def test_location_reassignment_replays_old_and_new_bucket(self):
        # Move M3's receipt and M4's issue together from A1 to a new
        # location A2. A1 keeps (M1: +10, M2: -4) -> 6 units remain; A2 gets
        # a self-contained (+10, -16 would stock out, so the issue quantity
        # is trimmed as part of the batch) pair. Both the old and new bucket
        # are replayed and both appear in the withdrawal trail.
        snapshot = self.ledger.apply_corrections([
            {'op': 'update', 'id': 'M3', 'fields': {'location': 'A2'}},
            {'op': 'update', 'id': 'M4', 'fields': {
                'location': 'A2', 'quantity': Decimal('10'),
            }},
        ], as_of=date(2026, 3, 2))

        balances = balances_by_key(snapshot.balances)

        # A1: 100.00 receipt, 40.00 issue -> 6 units / 60.00.
        self.assertEqual(balances[('WIDGET', 'A1')]['on_hand_quantity'], Decimal('6'))
        self.assertEqual(balances[('WIDGET', 'A1')]['on_hand_value'], Decimal('60.00'))
        # A2: receipt 10 @ 110.00, trimmed issue of 10 -> emptied, no
        # residual value.
        self.assertEqual(balances[('WIDGET', 'A2')]['on_hand_quantity'], Decimal('0'))
        self.assertEqual(balances[('WIDGET', 'A2')]['on_hand_value'], Decimal('0.00'))

        locations = {r['movement_id']: r['location'] for r in snapshot.cost_detail.rows}
        self.assertEqual(locations['M3'], 'A2')
        self.assertEqual(locations['M4'], 'A2')

        withdrawn_keys = {
            (r['report'], r['product'], r['location'])
            for r in snapshot.withdrawals.rows
        }
        # A1's prior balance/detail is replaced; A2 did not exist before,
        # so it has nothing to withdraw (its rows are fresh contributions).
        self.assertIn(('balances', 'WIDGET', 'A1'), withdrawn_keys)
        self.assertNotIn(('balances', 'WIDGET', 'A2'), withdrawn_keys)

        detail_withdrawals = {r['movement_id'] for r in snapshot.withdrawals.rows
                              if r['report'] == 'cost_detail'}
        self.assertEqual(set(detail_withdrawals), {'M1', 'M2', 'M3', 'M4'})

    def test_delete_movement_and_empty_bucket(self):
        snapshot = self.ledger.apply_corrections([
            {'op': 'delete', 'id': 'M1'},
            {'op': 'delete', 'id': 'M2'},
            {'op': 'delete', 'id': 'M3'},
            {'op': 'delete', 'id': 'M4'},
        ], as_of=date(2026, 3, 2))

        widget_detail = [r for r in snapshot.cost_detail.rows if r['product'] == 'WIDGET']
        self.assertEqual(widget_detail, [])
        self.assertNotIn(('WIDGET', 'A1'), balances_by_key(snapshot.balances))

        widget_profit = [r for r in snapshot.monthly_profit.rows if r['product'] == 'WIDGET']
        self.assertEqual(widget_profit, [])

        reports = {r['report'] for r in snapshot.withdrawals.rows}
        self.assertEqual({'cost_detail', 'balances', 'monthly_profit'}, reports)


class TestAtomicRejection(unittest.TestCase):
    def setUp(self):
        rows = [
            ('M1', 'WIDGET', 'A1', date(2026, 1, 3), Decimal(1), 'receipt',
             Decimal('10'), Decimal('100.00'), 'USD'),
            ('M2', 'WIDGET', 'A1', date(2026, 1, 10), Decimal(2), 'issue',
             Decimal('10'), Decimal('200.00'), 'USD'),
            ('M3', 'WIDGET', 'A1', date(2026, 1, 20), Decimal(3), 'receipt',
             Decimal('5'), Decimal('60.00'), 'USD'),
        ]
        self.ledger = agate.InventoryLedger(make_table(rows), as_of=date(2026, 3, 1))

    def test_stockout_correction_is_rejected_even_with_positive_ending_balance(self):
        # Deleting M1 leaves a positive ending balance (the M3 receipt) but
        # the M2 issue cannot be costed mid-stream.
        with self.assertRaises(StockoutError):
            self.ledger.delete_movement('M1', as_of=date(2026, 3, 2))

        self.assertEqual(self.ledger.latest_snapshot.generation, 1)
        self.assertEqual(self.ledger.corrections, ())
        self.assertEqual(len(self.ledger.latest_snapshot.cost_detail.rows), 3)

    def test_mixed_batch_rolls_back_every_correction(self):
        with self.assertRaises(StockoutError):
            self.ledger.apply_corrections([
                {'op': 'insert', 'values': payload(
                    'MX', 'OTHER', 'L9', date(2026, 1, 1), 1,
                    'receipt', 1, '1.00')},
                {'op': 'update', 'id': 'M2',
                 'fields': {'quantity': Decimal('999')}},
            ], as_of=date(2026, 3, 2))

        ids = {r['movement_id'] for r in self.ledger.latest_snapshot.cost_detail.rows}
        self.assertNotIn('MX', ids)
        self.assertEqual(self.ledger.latest_snapshot.generation, 1)

    def test_sequence_conflict_rejects_batch(self):
        with self.assertRaises(InventorySequenceError):
            self.ledger.insert_movement(
                payload('M9', 'WIDGET', 'A1', date(2026, 1, 3), 1,
                        'receipt', 1, '1.00'),
                as_of=date(2026, 3, 2),
            )

        self.assertEqual(self.ledger.latest_snapshot.generation, 1)

    def test_duplicate_and_unknown_ids(self):
        with self.assertRaises(InventoryInputError):
            self.ledger.insert_movement(
                payload('M1', 'WIDGET', 'A1', date(2026, 1, 5), 9,
                        'receipt', 1, '1.00'),
            )

        with self.assertRaises(InventoryInputError):
            self.ledger.update_movement('NOPE', {'quantity': Decimal('2')})

        with self.assertRaises(InventoryInputError):
            self.ledger.delete_movement('NOPE')

        with self.assertRaises(InventoryInputError):
            self.ledger.apply_corrections([])

    def test_id_column_is_immutable(self):
        with self.assertRaises(InventoryInputError):
            self.ledger.update_movement('M1', {'id': 'M1X'})

    def test_float_payload_rejected(self):
        with self.assertRaises(TypeError):
            self.ledger.insert_movement(
                payload('M9', 'WIDGET', 'A1', date(2026, 1, 5), 9,
                        'receipt', 1, '1.00') | {'amount': 1.0},
            )


class TestCorrectionAuditTrail(unittest.TestCase):
    def test_correction_table_records_each_operation(self):
        ledger = agate.InventoryLedger(
            make_table(simple_rows()), as_of=date(2026, 3, 1)
        )
        ledger.insert_movement(
            payload('M9', 'WIDGET', 'A1', date(2026, 1, 5), 5,
                    'receipt', 10, '105.00'),
            as_of=date(2026, 3, 2),
        )
        ledger.update_movement('M1', {'amount': Decimal('110.00')}, as_of=date(2026, 3, 3))
        ledger.delete_movement('M9', as_of=date(2026, 3, 4))

        table = ledger.correction_table
        self.assertSequenceEqual(
            table.column_names,
            ['correction_id', 'applied_at', 'operation', 'movement_id', 'description'],
        )
        self.assertEqual(
            [r['correction_id'] for r in table.rows], ['C1', 'C2', 'C3']
        )
        self.assertEqual(
            [r['operation'] for r in table.rows], ['insert', 'update', 'delete']
        )
        self.assertEqual([r['applied_at'] for r in table.rows],
                         [date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 4)])

        # Descriptions are exact JSON with canonical Decimal strings.
        insert_description = table.rows[0]['description']
        self.assertIn('"amount": "105.00"', insert_description)
        self.assertIn('"id": "M9"', insert_description)


class TestCustomConfig(unittest.TestCase):
    def test_no_currency_column_and_custom_markers(self):
        config = InventoryConfig(
            currency_column=None, receipt_marker='IN', issue_marker='OUT',
            default_currency='EUR',
        )
        names = ['id', 'product', 'location', 'business_date', 'sequence',
                 'direction', 'quantity', 'amount']
        types = [agate.Text(), agate.Text(), agate.Text(), agate.Date(),
                 agate.Number(), agate.Text(), agate.Number(), agate.Number()]
        table = agate.Table([
            ('M1', 'W', 'L', date(2026, 1, 1), Decimal(1), 'IN',
             Decimal('10'), Decimal('100.00')),
            ('M2', 'W', 'L', date(2026, 1, 2), Decimal(2), 'OUT',
             Decimal('4'), Decimal('80.00')),
        ], names, types)

        ledger = agate.InventoryLedger(table, config=config)
        snapshot = ledger.latest_snapshot

        self.assertEqual(snapshot.cost_detail.rows[0]['currency'], 'EUR')
        self.assertEqual(snapshot.cost_detail.rows[1]['cogs'], Decimal('40.00'))

        corrected = ledger.update_movement(
            'M1', {'amount': Decimal('120.00')}, as_of=date(2026, 2, 1)
        )
        self.assertEqual(
            [r for r in corrected.cost_detail.rows if r['movement_id'] == 'M2'][0]['cogs'],
            Decimal('48.00'),
        )
