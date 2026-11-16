import unittest
from decimal import Decimal

from agate.inventory.config import InventoryConfig, as_decimal, quantize_money


class TestQuantizeMoney(unittest.TestCase):
    def test_half_even_bankers_rounding(self):
        # 2.345 -> 2.34 and 2.365 -> 2.36 round to even; 2.355/2.375 round up.
        cases = {
            '2.345': '2.34',
            '2.355': '2.36',
            '2.365': '2.36',
            '2.375': '2.38',
            '-2.345': '-2.34',
            '-2.355': '-2.36',
        }

        for source, expected in cases.items():
            self.assertEqual(
                quantize_money(Decimal(source), 2),
                Decimal(expected),
                source,
            )

    def test_zero_decimal_currency(self):
        self.assertEqual(quantize_money(Decimal('2.5'), 0), Decimal('2'))
        self.assertEqual(quantize_money(Decimal('3.5'), 0), Decimal('4'))

    def test_three_decimal_currency(self):
        self.assertEqual(quantize_money(Decimal('1.2345'), 3), Decimal('1.234'))
        self.assertEqual(quantize_money(Decimal('1.2355'), 3), Decimal('1.236'))

    def test_rejects_float(self):
        with self.assertRaises(TypeError):
            quantize_money(2.5, 2)


class TestAsDecimal(unittest.TestCase):
    def test_passthrough_and_int(self):
        self.assertEqual(as_decimal(Decimal('1.5'), 'x'), Decimal('1.5'))
        self.assertEqual(as_decimal(3, 'x'), Decimal(3))

    def test_nullable(self):
        self.assertIsNone(as_decimal(None, 'x', nullable=True))

        with self.assertRaises(ValueError):
            as_decimal(None, 'x')

    def test_rejects_float_and_bool(self):
        with self.assertRaises(TypeError):
            as_decimal(1.0, 'x')

        with self.assertRaises(TypeError):
            as_decimal(True, 'x')


class TestInventoryConfig(unittest.TestCase):
    def test_default_minor_units(self):
        config = InventoryConfig()
        self.assertEqual(config.minor_unit('USD'), 2)
        self.assertEqual(config.minor_unit('JPY'), 2)

    def test_minor_unit_mapping(self):
        config = InventoryConfig(minor_units={'USD': 2, 'JPY': 0})
        self.assertEqual(config.minor_unit('JPY'), 0)
        self.assertEqual(config.minor_unit('USD'), 2)

        with self.assertRaises(KeyError):
            config.minor_unit('XXX')

    def test_required_columns_optional_currency(self):
        config = InventoryConfig(currency_column=None)
        self.assertNotIn('currency', config.required_columns())
        self.assertEqual(len(config.required_columns()), 8)

        self.assertIn('currency', InventoryConfig().required_columns())

    def test_custom_names_and_markers(self):
        config = InventoryConfig(
            id_column='mvmt', product_column='sku', location_column='wh',
            date_column='d', sequence_column='ord', direction_column='kind',
            quantity_column='qty', amount_column='amt', currency_column=None,
            receipt_marker='IN', issue_marker='OUT',
        )
        types = config.expected_input_types()
        self.assertEqual(set(types), set(config.required_columns()))
        self.assertNotIn('currency', types)
