"""
Exceptions raised by the :mod:`agate.inventory` package.

All inventory accounting is *all-or-nothing*: any error raised while building a
:class:`~agate.inventory.InventorySnapshot` (including an attempted correction)
aborts the calculation before any new snapshot is published, so the previously
accepted snapshot is never partially overwritten.
"""


class InventoryAccountingError(Exception):
    """
    Base class for every error raised by the inventory ledger.
    """


class InventoryInputError(InventoryAccountingError):
    """
    A movement table or a correction payload does not satisfy the ledger's
    input contract (missing column, wrong native type, duplicate or unknown
    identifier, non-positive quantity, receipt without an amount, ...).
    """


class InventorySequenceError(InventoryAccountingError):
    """
    Two movements of the same product/location occupy the same business
    position (date and sequence) and therefore have no unambiguous order.
    """


class StockoutError(InventoryAccountingError):
    """
    An issue would drive the on-hand quantity below zero.

    Because stockouts make a moving-average issue cost undefined, the whole
    batch (initial accounting or correction) is rejected, even if a later
    receipt would eventually leave a positive ending balance.
    """
    def __init__(self, movement_id, product, location, business_date, on_hand, requested):
        self.movement_id = movement_id
        self.product = product
        self.location = location
        self.business_date = business_date
        self.on_hand = on_hand
        self.requested = requested

        super().__init__(
            'Movement "%s" (product "%s", location "%s", %s) requests %s but only %s is on hand; '
            'the batch is rejected because an issue cannot be costed while stock is insufficient.'
            % (movement_id, product, location, business_date, requested, on_hand)
        )
