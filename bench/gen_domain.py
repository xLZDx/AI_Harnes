"""Domain benchmark: code that is technically clean but wrong by the rules of the domain. Key lines are derived from anchors."""
import json
from pathlib import Path
HERE = Path(__file__).parent
DB = HERE / "dombench"
C = {}

C["d01"] = ("ledger/posting.py", '''from dataclasses import dataclass
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
''', [
    ("D1", "self.store.insert(entry)", -6, 1, "MAJOR", "entry is posted without checking that total debit equals total credit",
     r"balanc|debit.{0,40}credit|credit.{0,40}debit|equal|double.?entry|sum"),
    ("D2", "lines = [Line(l.account, l.debit, l.credit)", 0, 0, "MAJOR", "reversal copies debit/credit instead of swapping them, so it doubles the original",
     r"swap|revers|invert|opposite|same (sign|side|direction)|doubl|negat"),
    ("D3", "def post(self, entry: Entry)", 0, 8, "MAJOR", "posting into a closed accounting period is not prevented (periods.is_closed never called)",
     r"closed|period|is_closed|lock")])

C["d02"] = ("treasury/revaluation.py", '''from decimal import Decimal, ROUND_HALF_UP

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
''', [
    ("D1", 'rate = rates.get(item["currency"], item["doc_date"])', 0, 0, "MAJOR", "revaluation uses the document-date rate instead of the period-end (closing) rate, so the difference is always zero",
     r"period.?end|closing|doc_date|always zero|no.?op|never|same rate"),
    ("D2", 'diff = (revalued - carrying)', -1, 2, "MAJOR", "sign is not inverted for payables: a rate increase is a loss on AP but is reported as a gain",
     r"payabl|\bAP\b|sign|liabilit|loss"),
    ("D3", "return receivable_balance + credit_note_amount", 0, 0, "MAJOR", "credit note increases the receivable instead of reducing it",
     r"reduc|decreas|subtract|minus|sign|increas|wrong direction")])

C["d03"] = ("training/progression.py", '''from dataclasses import dataclass


@dataclass
class Profile:
    level: str            # "beginner" | "intermediate" | "advanced"
    age: int
    injuries: list        # free-text injury flags entered by the user
    symptoms: list        # symptom flags from the daily check-in, e.g. "chest_pain", "dizziness", "sore_legs"


@dataclass
class Week:
    volume_min: int       # planned minutes of training
    sessions: int


def next_week(profile: Profile, last: Week, completed_ratio: float) -> Week:
    """Plan next week's load from last week's plan and how much of it the user completed."""
    if profile.injuries:
        return Week(volume_min=int(last.volume_min * 0.8), sessions=last.sessions)
    if completed_ratio >= 0.8:
        growth = 1.25
    else:
        growth = 1.0
    sessions = min(last.sessions + 1, 7)
    return Week(volume_min=int(last.volume_min * growth), sessions=sessions)
''', [
    ("D1", "growth = 1.25", 0, 0, "MAJOR", "load grows 25 % per week for every level including beginners (far above the ~10 % rule), and sessions also go up every week to 7 with no rest day",
     r"25|10 ?%|progress|too (fast|aggressive|steep)|overload|overtrain|beginner|rest day|7 sessions|recovery"),
    ("D2", "if profile.injuries:", 0, 1, "BLOCKER", "red-flag symptoms (chest pain, dizziness) are ignored: training continues and even increases instead of stopping and referring to a doctor",
     r"chest|dizz|symptom|red.?flag|cardiac|medical|refer|doctor|stop")])

C["d04"] = ("nutrition/targets.py", '''def bmr_mifflin(sex: str, weight_kg: float, height_cm: float, age: int) -> float:
    """Basal metabolic rate, Mifflin-St Jeor."""
    base = 10 * weight_kg + 6.25 * height_cm - 5 * age
    return base - 161 if sex == "male" else base + 5


ACTIVITY = {"sedentary": 1.2, "light": 1.375, "moderate": 1.55, "high": 1.725}


def daily_targets(sex, weight_kg, height_cm, age, activity, goal):
    tdee = bmr_mifflin(sex, weight_kg, height_cm, age) * ACTIVITY[activity]
    if goal == "lose":
        kcal = tdee - 1000
    elif goal == "gain":
        kcal = tdee + 300
    else:
        kcal = tdee
    protein_g = 0.8 * weight_kg if goal != "gain" else 1.0 * weight_kg
    return {"kcal": round(kcal), "protein_g": round(protein_g)}
''', [
    ("D1", 'return base - 161 if sex == "male"', 0, 0, "MAJOR", "Mifflin-St Jeor sex constants are swapped (+5 is male, -161 is female)",
     r"swap|revers|male|female|\+ ?5|161|constant"),
    ("D2", "kcal = tdee - 1000", 0, 0, "MAJOR", "fixed 1000 kcal deficit with no floor can drop small users below a safe intake (e.g. under 1200 kcal)",
     r"1000|floor|minimum|1200|1500|unsafe|too (low|large|aggressive)|deficit|below"),
    ("D3", "protein_g = 0.8 * weight_kg", 0, 0, "MINOR", "0.8 g/kg protein (sedentary RDA) is too low for training users, especially in a deficit or for gain (1.6-2.2 g/kg)",
     r"0\\.8|1\\.6|2\\.2|protein|RDA|too low|insufficient")])

C["d05"] = ("research/evaluate.py", '''import numpy as np
from sklearn.model_selection import KFold


def annualized_sharpe(daily_returns: np.ndarray) -> float:
    """Sharpe ratio of a 24/7 crypto strategy from daily returns."""
    mu, sd = daily_returns.mean(), daily_returns.std(ddof=1)
    return 0.0 if sd == 0 else float(mu / sd * np.sqrt(252))


def cross_validate(model, X: np.ndarray, y: np.ndarray, n_splits: int = 5) -> list[float]:
    """X, y are rows of hourly bars in time order; labels look 24 bars ahead."""
    scores = []
    for train_idx, test_idx in KFold(n_splits=n_splits, shuffle=True, random_state=7).split(X):
        model.fit(X[train_idx], y[train_idx])
        scores.append(model.score(X[test_idx], y[test_idx]))
    return scores


def position_size(equity: float, risk_fraction: float, entry: float, stop: float, contract_size: float) -> float:
    """Number of contracts so that hitting the stop loses risk_fraction of equity."""
    return equity * risk_fraction / abs(entry - stop)
''', [
    ("D1", "np.sqrt(252)", 0, 0, "MAJOR", "24/7 crypto annualised with sqrt(252) trading days instead of sqrt(365)",
     r"252|365|24/7|annuali|calendar"),
    ("D2", "KFold(n_splits=n_splits, shuffle=True", 0, 0, "BLOCKER", "shuffled K-fold on time-ordered data with 24-bar forward labels leaks the future (needs purged/embargoed walk-forward)",
     r"shuffl|leak|look.?ahead|purg|embargo|walk.?forward|time.?series|future"),
    ("D3", "return equity * risk_fraction / abs(entry - stop)", 0, 0, "MAJOR", "contract_size is ignored, so the position is off by the contract multiplier",
     r"contract_size|multiplier|ignored|unused")])

key = {}
for code, (rel, src, defects) in C.items():
    p = DB / code / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(src, encoding="utf-8", newline="\n")
    lines = src.split("\n")
    ds = []
    for did, anchor, lo, hi, sev, what, kw in defects:
        hit = [i + 1 for i, l in enumerate(lines) if anchor in l]
        assert hit, (code, did, anchor)
        ds.append({"id": did, "file": Path(rel).name, "lines": [hit[0] + lo, hit[0] + hi], "min_severity": sev, "what": what, "keywords": kw})
    key[code] = {"defects": ds}
(HERE / "dombench_key.json").write_text(json.dumps(key, indent=1), encoding="utf-8")
print({k: [d["lines"] for d in v["defects"]] for k, v in key.items()})
