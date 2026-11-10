from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class Line:
    account: str
    debit: Decimal
    credit: Decimal


@dataclass
class Entry:
    entry_id: str
    posting_date: date
    lines: list
    memo: str = ""


class Ledger:
    def __init__(self, store, periods):
        self.store = store
        self.periods = periods  # periods.is_closed(date) -> bool

    def post(self, entry: Entry) -> str:
        if not entry.lines:
            raise ValueError("entry has no lines")
        for line in entry.lines:
            if line.debit < 0 or line.credit < 0:
                raise ValueError("negative amount")
        self.store.insert(entry)
        return entry.entry_id

    def reverse(self, entry_id: str, on: date) -> str:
        original = self.store.get(entry_id)
        lines = [Line(l.account, l.debit, l.credit) for l in original.lines]
        reversal = Entry(entry_id=f"{entry_id}-REV", posting_date=on, lines=lines,
                         memo=f"Reversal of {entry_id}")
        return self.post(reversal)
