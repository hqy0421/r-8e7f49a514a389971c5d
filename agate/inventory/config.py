"""
Configuration and numeric primitives for perpetual moving-average accounting.

Every monetary value handled by this package is a :class:`decimal.Decimal`
holding an integer multiple of the agreed currency's smallest unit (for
example, euro cents when ``minor_unit=2``). Quantization always uses
banker's rounding (``ROUND_HALF_EVEN``) and is applied once per movement,
never to an intermediate running total. :class:`float` values are rejected
up front so that binary floating point can never enter the calculation.
"""

from decimal import ROUND_HALF_EVEN, Decimal

from agate.data_types import Date, DateTime, Number, Text

#: Rounding mode used for every monetary quantization in this package.
BANKERS_ROUNDING = ROUND_HALF_EVEN

ZERO = Decimal('0')
ONE = Decimal('1')


def quantize_money(value, minor_unit):
    """
    Round ``value`` to the smallest unit of the agreed currency using
    half-even (banker's) rounding.

    :param value:
        A :class:`decimal.Decimal`. Other numeric types -- in particular
        :class:`float` -- are rejected.
    :param minor_unit:
        Number of digits after the decimal separator of the currency's
        smallest unit (``2`` for cents, ``0`` for a zero-decimal currency).
    """
    if not isinstance(value, Decimal):
        raise TypeError(
            'Inventory accounting only accepts decimal.Decimal values, got %s. '
            'Build Decimals from strings or integers, never from float.'
            % type(value).__name__
        )

    return value.quantize(Decimal(1).scaleb(-minor_unit), rounding=BANKERS_ROUNDING)


def as_decimal(value, column_name, *, nullable=False):
    """
    Return ``value`` as a :class:`decimal.Decimal`, rejecting :class:`float`.

    Agate's :class:`~agate.data_types.Number` type already stores Decimals,
    so this is mainly a defensive guard for hand-built inputs and correction
    payloads.
    """
    if value is None:
        if nullable:
            return None

        raise ValueError('Column "%s" contains a null but a value is required.' % column_name)

    if isinstance(value, Decimal):
        return value

    if isinstance(value, bool):  # bool is an int subclass; reject explicitly
        raise TypeError('Column "%s" must be Decimal, not bool.' % column_name)

    if isinstance(value, int):
        return Decimal(value)

    if isinstance(value, float):
        raise TypeError(
            'Column "%s" contains a float (%r). Convert it to Decimal via a string or integer first; '
            'inventory values must never pass through binary floating point.' % (column_name, value)
        )

    raise TypeError('Column "%s" must be Decimal, got %s.' % (column_name, type(value).__name__))


class InventoryConfig:
    """
    Column-name mapping, direction markers and currency precision for a
    :class:`~agate.inventory.InventoryLedger`.

    The defaults describe a movement table with these columns:

    ``id``, ``product``, ``location``, ``business_date``, ``sequence``,
    ``direction``, ``quantity``, ``amount``, ``currency``.

    :param id_column:
        Name of the column holding the stable identifier of each movement.
        Corrections refer to movements through this identifier.
    :param product_column:
        Name of the product (SKU) column.
    :param location_column:
        Name of the storage location column.
    :param date_column:
        Name of the business-date column. Values must be native
        :class:`datetime.date` or :class:`datetime.datetime` (agate
        :class:`~agate.data_types.Date`/:class:`~agate.data_types.DateTime`).
    :param sequence_column:
        Name of the ordering column. Movements of one product/location are
        processed by ``(date, sequence)``. Values must be integers.
    :param direction_column:
        Name of the column marking receipts versus issues.
    :param quantity_column:
        Name of the column holding the affected quantity, always a positive
        :class:`decimal.Decimal`; the direction column decides the sign.
    :param amount_column:
        Name of the amount column. On a receipt it is the total acquisition
        cost in the agreed currency; on an issue it is the net sales
        revenue (may be null when gross margin cannot be computed).
    :param currency_column:
        Optional name of the currency column. When present, each currency
        gets its own precision through ``minor_units`` and movements of
        different currencies are never averaged together. May be ``None``
        for a single-currency ledger identified by ``default_currency``.
    :param receipt_marker:
        Value in the direction column that denotes a goods receipt.
    :param issue_marker:
        Value in the direction column that denotes a goods issue (sale or
        other outbound movement).
    :param minor_units:
        Mapping of currency code to the number of decimal places of its
        smallest unit. A plain integer sets the precision for every
        currency.
    :param default_currency:
        Currency code used when there is no currency column.
    """
    def __init__(
        self,
        id_column='id',
        product_column='product',
        location_column='location',
        date_column='business_date',
        sequence_column='sequence',
        direction_column='direction',
        quantity_column='quantity',
        amount_column='amount',
        currency_column='currency',
        receipt_marker='receipt',
        issue_marker='issue',
        minor_units=2,
        default_currency='XXX',
    ):
        self.id_column = id_column
        self.product_column = product_column
        self.location_column = location_column
        self.date_column = date_column
        self.sequence_column = sequence_column
        self.direction_column = direction_column
        self.quantity_column = quantity_column
        self.amount_column = amount_column
        self.currency_column = currency_column
        self.receipt_marker = receipt_marker
        self.issue_marker = issue_marker
        self.default_currency = default_currency

        if isinstance(minor_units, int):
            self._minor_units = None
            self._default_minor_unit = minor_units
        else:
            self._minor_units = dict(minor_units)
            self._default_minor_unit = self._minor_units.get(default_currency, 2)

    def minor_unit(self, currency):
        """
        Number of minor-unit digits for ``currency``.
        """
        if self._minor_units is None:
            return self._default_minor_unit

        try:
            return self._minor_units[currency]
        except KeyError:
            raise KeyError(
                'No minor-unit precision is configured for currency "%s"; add it to minor_units.' % currency
            )

    def required_columns(self):
        """
        Column names that must exist in every movement table.
        """
        names = [
            self.id_column,
            self.product_column,
            self.location_column,
            self.date_column,
            self.sequence_column,
            self.direction_column,
            self.quantity_column,
            self.amount_column,
        ]

        if self.currency_column:
            names.append(self.currency_column)

        return names

    def expected_input_types(self):
        """
        Mapping of required column name to the native agate data type that
        the column must use. Text/date columns accept their natural Python
        values; number columns must be agate :class:`~agate.data_types.Number`
        so values arrive as :class:`decimal.Decimal`.
        """
        types = {
            self.id_column: Text,
            self.product_column: Text,
            self.location_column: Text,
            self.date_column: (Date, DateTime),
            self.sequence_column: Number,
            self.direction_column: Text,
            self.quantity_column: Number,
            self.amount_column: Number,
        }

        if self.currency_column:
            types[self.currency_column] = Text

        return types
