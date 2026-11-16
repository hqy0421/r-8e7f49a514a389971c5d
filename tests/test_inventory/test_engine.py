import unittest
from datetime import date
from decimal import Decimal

import agate
from agate.inventory.config import InventoryConfig
from agate.inventory.engine import extract_movements, replay_key, summarize_monthly_profit
from agate.inventory.exceptions import InventoryInputError, InventorySequenceError, StockoutError
from tests.test_inventory.helpers import COLUMN_NAMES, COLUMN_TYPES, make_table, simple_rows


class TestExtractMovements(unittest.TestCase):
    def test_groups_and_orders_by_key(self):
        table = make_table(simple_rows())
        by_key, by_id = extract_movements(table, InventoryConfig())

        self.assertEqual(set(by_key), {('WIDGET', 'A1')})
        self.assertEqual([m.id for m in by_key[('WIDGET', 'A1')]], ['M1', 'M2', 'M3', 'M4'])
        self.assertEqual(set(by_id), {'M1', 'M2', 'M3', 'M4'})
        self.assertIs(by_id['M1'], by_key[('WIDGET', 'A1')][0])

    def test_rejects_duplicate_id(self):
        rows = simple_rows()
        rows[1] = tuple(['M1'] + list(rows[1])[1:])

        with self.assertRaises(InventoryInputError):
            extract_movements(make_table(rows), InventoryConfig())

    def test_rejects_missing_column(self):
        table = make_table(
            [row[:-1] for row in simple_rows()],
            COLUMN_NAMES[:-1],
            COLUMN_TYPES[:-1],
        )

        with self.assertRaises(InventoryInputError):
            extract_movements(table, InventoryConfig())

    def test_rejects_non_native_number_type(self):
        # A Text column where Number (Decimal) is required must be refused,
        # because the ledger must keep real native types.
        types = list(COLUMN_TYPES)
        types[6] = agate.Text()

        table = agate.Table(
            [tuple(str(v) for v in row) for row in simple_rows()],
            COLUMN_NAMES,
            types,
        )

        with self.assertRaises(InventoryInputError):
            extract_movements(table, InventoryConfig())

    def test_rejects_non_positive_quantity(self):
        rows = simple_rows()
        rows[0] = tuple(list(rows[0])[:6] + [Decimal('0'), Decimal('100.00'), 'USD'])

        with self.assertRaises(InventoryInputError):
            extract_movements(make_table(rows), InventoryConfig())

    def test_rejects_receipt_without_amount(self):
        rows = simple_rows()
        rows[0] = tuple(list(rows[0])[:7] + [None, 'USD'])

        with self.assertRaises(InventoryInputError):
            extract_movements(make_table(rows), InventoryConfig())

    def test_rejects_unknown_direction(self):
        rows = simple_rows()
        rows[0] = tuple(list(rows[0])[:5] + ['adjustment'] + list(rows[0])[6:])

        with self.assertRaises(InventoryInputError):
            extract_movements(make_table(rows), InventoryConfig())

    def test_rejects_ambiguous_position(self):
        rows = simple_rows()
        # Give M2 the same (date, sequence) as M1.
        rows[1] = (
            'M2', 'WIDGET', 'A1', date(2026, 1, 3), Decimal(1), 'issue',
            Decimal('4'), Decimal('80.00'), 'USD',
        )

        with self.assertRaises(InventorySequenceError):
            extract_movements(make_table(rows), InventoryConfig())

    def test_rejects_mixed_currency_bucket(self):
        rows = simple_rows()
        rows[1] = tuple(list(rows[1])[:-1] + ['EUR'])

        with self.assertRaises(InventoryInputError):
            extract_movements(make_table(rows), InventoryConfig())

    def test_rejects_non_integer_sequence(self):
        rows = simple_rows()
        rows[0] = tuple(list(rows[0])[:4] + [Decimal('1.5')] + list(rows[0])[5:])

        with self.assertRaises(InventoryInputError):
            extract_movements(make_table(rows), InventoryConfig())


class TestReplayKey(unittest.TestCase):
    def setUp(self):
        self.config = InventoryConfig()
        by_key, _ = extract_movements(make_table(simple_rows()), self.config)
        self.movements = by_key[('WIDGET', 'A1')]

    def test_moving_average_receipts_and_issues(self):
        detail, balance = replay_key(self.movements, 1, self.config)

        m1, m2, m3, m4 = detail

        # Receipt: running average 100 / 10.
        self.assertEqual(m1[8], Decimal('10.000'))
        self.assertEqual(m1[9], Decimal('100.00'))
        self.assertIsNone(m1[10])
        self.assertEqual(m1[13], Decimal('10'))
        self.assertEqual(m1[14], Decimal('100.00'))

        # Issue at the average currently on hand: 4 * 10 = 40.
        self.assertEqual(m2[10], Decimal('40.00'))
        self.assertEqual(m2[11], Decimal('80.00'))
        self.assertEqual(m2[12], Decimal('40.00'))
        self.assertEqual(m2[13], Decimal('6'))
        self.assertEqual(m2[14], Decimal('60.00'))

        # New receipt blends the average: 170 / 16 = 10.625.
        self.assertEqual(m3[8], Decimal('10.625'))
        self.assertEqual(m3[14], Decimal('170.00'))

        # Emptying issue absorbs the full residual value.
        self.assertEqual(m4[10], Decimal('170.00'))
        self.assertEqual(m4[13], Decimal('0'))
        self.assertEqual(m4[14], Decimal('0.00'))
        self.assertEqual(m4[15], Decimal('0.000'))

        # Ending balance of an emptied stock is zero quantity and zero value.
        self.assertEqual(balance[3], Decimal('0'))
        self.assertEqual(balance[4], Decimal('0.00'))
        self.assertEqual(balance[5], Decimal('0'))

    def test_generation_is_stamped(self):
        detail, balance = replay_key(self.movements, 7, self.config)
        self.assertTrue(all(row[-1] == Decimal(7) for row in detail))
        self.assertEqual(balance[-1], Decimal(7))

    def test_stockout_rejects_whole_replay(self):
        rows = [
            ('R1', 'P', 'L', date(2026, 1, 1), Decimal(1), 'receipt',
             Decimal('5'), Decimal('50.00'), 'USD'),
            ('I1', 'P', 'L', date(2026, 1, 2), Decimal(2), 'issue',
             Decimal('6'), None, 'USD'),
        ]
        by_key, _ = extract_movements(make_table(rows), self.config)

        with self.assertRaises(StockoutError) as ctx:
            replay_key(by_key[('P', 'L')], 1, self.config)

        self.assertEqual(ctx.exception.movement_id, 'I1')
        self.assertEqual(ctx.exception.on_hand, Decimal('5'))
        self.assertEqual(ctx.exception.requested, Decimal('6'))

    def test_stockout_even_when_later_receipt_would_recover(self):
        # Ending balance would be positive, but the mid-stream issue cannot
        # be costed: the replay must still raise.
        rows = [
            ('R1', 'P', 'L', date(2026, 1, 1), Decimal(1), 'receipt',
             Decimal('5'), Decimal('50.00'), 'USD'),
            ('I1', 'P', 'L', date(2026, 1, 2), Decimal(2), 'issue',
             Decimal('6'), None, 'USD'),
            ('R2', 'P', 'L', date(2026, 1, 3), Decimal(3), 'receipt',
             Decimal('10'), Decimal('100.00'), 'USD'),
        ]
        by_key, _ = extract_movements(make_table(rows), self.config)

        with self.assertRaises(StockoutError):
            replay_key(by_key[('P', 'L')], 1, self.config)

    def test_half_even_cogs_with_residual_dust(self):
        rows = [
            ('R1', 'P', 'L', date(2026, 1, 1), Decimal(1), 'receipt',
             Decimal('3'), Decimal('10.00'), 'USD'),
            ('I1', 'P', 'L', date(2026, 1, 2), Decimal(2), 'issue',
             Decimal('1'), Decimal('5.00'), 'USD'),
            ('I2', 'P', 'L', date(2026, 1, 3), Decimal(3), 'issue',
             Decimal('2'), Decimal('9.00'), 'USD'),
        ]
        by_key, _ = extract_movements(make_table(rows), self.config)
        detail, balance = replay_key(by_key[('P', 'L')], 1, self.config)

        # 10 / 3 = 3.333... -> 3.33; residual 6.67 is fully absorbed when
        # the stock is emptied.
        self.assertEqual(detail[1][10], Decimal('3.33'))
        self.assertEqual(detail[2][10], Decimal('6.67'))
        self.assertEqual(detail[2][14], Decimal('0.00'))
        self.assertEqual(balance[4], Decimal('0.00'))

    def test_null_revenue_issue_keeps_cogs_but_no_margin(self):
        rows = [
            ('R1', 'P', 'L', date(2026, 1, 1), Decimal(1), 'receipt',
             Decimal('2'), Decimal('10.00'), 'USD'),
            ('I1', 'P', 'L', date(2026, 1, 2), Decimal(2), 'issue',
             Decimal('1'), None, 'USD'),
        ]
        by_key, _ = extract_movements(make_table(rows), self.config)
        detail, _ = replay_key(by_key[('P', 'L')], 1, self.config)

        self.assertEqual(detail[1][10], Decimal('5.00'))
        self.assertIsNone(detail[1][11])
        self.assertIsNone(detail[1][12])

    def test_zero_decimal_currency(self):
        config = InventoryConfig(minor_units={'JPY': 0})
        rows = [
            ('R1', 'P', 'L', date(2026, 1, 1), Decimal(1), 'receipt',
             Decimal('3'), Decimal('100'), 'JPY'),
            ('I1', 'P', 'L', date(2026, 1, 2), Decimal(2), 'issue',
             Decimal('1'), Decimal('40'), 'JPY'),
        ]
        by_key, _ = extract_movements(make_table(rows), config)
        detail, balance = replay_key(by_key[('P', 'L')], 1, config)

        self.assertEqual(detail[1][10], Decimal('33'))
        self.assertEqual(balance[4], Decimal('67'))


class TestSummarizeMonthlyProfit(unittest.TestCase):
    def test_aggregates_issues_per_product_and_month(self):
        config = InventoryConfig()
        by_key, _ = extract_movements(make_table(simple_rows()), config)
        detail, _ = replay_key(by_key[('WIDGET', 'A1')], 1, config)
        summary = summarize_monthly_profit(detail, 1)

        self.assertEqual(len(summary), 2)

        january, february = summary
        self.assertEqual(january[0], 'WIDGET')
        self.assertEqual((january[2], january[3]), (Decimal(2026), Decimal(1)))
        self.assertEqual(january[4], Decimal('4'))
        self.assertEqual(january[5], Decimal('80.00'))
        self.assertEqual(january[6], Decimal('40.00'))
        self.assertEqual(january[7], Decimal('40.00'))

        self.assertEqual((february[2], february[3]), (Decimal(2026), Decimal(2)))
        self.assertEqual(february[4], Decimal('16'))
        self.assertEqual(february[7], Decimal('190.00'))

    def test_null_revenue_bucket_has_null_margin(self):
        rows = [
            ('R1', 'P', 'L', date(2026, 1, 1), Decimal(1), 'receipt',
             Decimal('2'), Decimal('10.00'), 'USD'),
            ('I1', 'P', 'L', date(2026, 1, 2), Decimal(2), 'issue',
             Decimal('1'), None, 'USD'),
        ]
        config = InventoryConfig()
        by_key, _ = extract_movements(make_table(rows), config)
        detail, _ = replay_key(by_key[('P', 'L')], 1, config)
        summary = summarize_monthly_profit(detail, 1)

        self.assertEqual(len(summary), 1)
        self.assertIsNone(summary[0][5])
        self.assertIsNone(summary[0][7])
        self.assertEqual(summary[0][6], Decimal('5.00'))
