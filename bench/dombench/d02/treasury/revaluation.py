from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.01")


def revalue_open_items(items, rates, period_end):
    """Month-end revaluation of open foreign-currency receivables and payables.

    items: dicts with currency, amount_fc (open amount in foreign currency), booked_rate (rate used when the
           document was posted), doc_date, kind ("AR" or "AP").
    rates: rates.get(currency, date) -> Decimal, local currency per 1 unit of foreign currency.
    Returns adjustment lines in local currency; positive means a gain.
    """
    out = []
    for item in items:
        rate = rates.get(item["currency"], item["doc_date"])
        carrying = item["amount_fc"] * item["booked_rate"]
        revalued = item["amount_fc"] * rate
        diff = (revalued - carrying).quantize(CENT, ROUND_HALF_UP)
        out.append({"doc": item["doc"], "kind": item["kind"], "fx_difference": diff})
    return out


def apply_credit_note(receivable_balance: Decimal, credit_note_amount: Decimal) -> Decimal:
    """Return the customer's receivable after a credit note is issued."""
    return receivable_balance + credit_note_amount
