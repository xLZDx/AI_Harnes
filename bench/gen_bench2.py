"""Generate benchmark v2: harder reviewer cases (2 seeded defects + 1 trap each) and tester cases.
Answer keys go to bench2_key.json OUTSIDE the agent-visible directories."""
import json
from pathlib import Path

ROOT = Path(__file__).parent
B2 = ROOT / "bench2"
T2 = ROOT / "tbench"
HID = ROOT / "tbench_hidden"   # correct + variant implementations, never shown to agents

R = {}
R["v01"] = ("src/billing/refunds.py", '''from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN

CENT = Decimal("0.01")


def to_money(value) -> Decimal:
    """Convert user input to a 2-decimal Decimal without float noise."""
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_EVEN)


@dataclass
class Payment:
    payment_id: str
    captured: Decimal
    currency: str
    refunds: list = field(default_factory=list)

    @property
    def refunded_total(self) -> Decimal:
        return sum((r["amount"] for r in self.refunds), Decimal("0"))


class RefundService:
    def __init__(self, gateway, store):
        self.gateway = gateway
        self.store = store

    def refund(self, payment: Payment, amount, reason: str) -> dict:
        amount = to_money(amount)
        if amount <= 0:
            raise ValueError("refund amount must be positive")
        if amount > payment.captured:
            raise ValueError("refund exceeds captured amount")
        key = f"refund:{payment.payment_id}:{amount}:{datetime.now(timezone.utc).isoformat()}"
        result = self.gateway.refund(payment.payment_id, str(amount), idempotency_key=key)
        payment.refunds.append({"amount": amount, "reason": reason, "id": result["id"]})
        self.store.save(payment)
        return result

    def refund_all(self, payment: Payment, reason: str) -> dict | None:
        remaining = payment.captured - payment.refunded_total
        if remaining == 0:
            return None
        return self.refund(payment, remaining, reason)
''', [
    {"id": "D1", "lines": [33, 35], "min_severity": "MAJOR", "anchor": "if amount > payment.captured:",
     "keywords": r"refunded_total|already.refunded|previous|prior|cumulative|over.?refund|remaining|total refund|multiple (partial )?refunds|sum of refunds"},
    {"id": "D2", "lines": [35, 37], "min_severity": "MAJOR", "anchor": "key = f\"refund:",
     "keywords": r"idempot|timestamp|datetime|now\(|duplicate|double|retry|retried"}],
  [{"lines": [8, 10], "keywords": r"float|precision|Decimal\(str|quantiz|rounding|ROUND_HALF"},
   {"lines": [20, 22], "keywords": r"sum\(|start value|refunded_total"}])

R["v02"] = ("src/sync/worker.py", '''import logging
import random
import time

log = logging.getLogger(__name__)


class TransientError(Exception):
    """Network or 5xx error from the upstream API; safe to retry."""


def backoff(attempt: int, base: float = 0.5, cap: float = 30.0) -> float:
    return min(cap, base * 2 ** attempt) * random.uniform(0.5, 1.0)


def fetch_with_retry(client, cursor, max_attempts=5):
    attempt = 0
    while True:
        try:
            return client.fetch(after=cursor, limit=500)
        except Exception as exc:
            attempt += 1
            log.warning("fetch failed (attempt %s): %s", attempt, exc)
            time.sleep(backoff(attempt))


def sync_once(client, repo, state):
    cursor = state.load_cursor()
    page = fetch_with_retry(client, cursor)
    if not page.items:
        return 0
    state.save_cursor(page.next_cursor)
    for item in page.items:
        repo.upsert(external_id=item["id"], payload=item)
    return len(page.items)
''', [
    {"id": "D1", "lines": [16, 24], "min_severity": "MAJOR", "anchor": "while True:",
     "keywords": r"max_attempts|infinite|forever|never (stop|give|terminat)|unbounded|endless|non.?transient|TransientError|every exception|all exceptions|any exception|permanent"},
    {"id": "D2", "lines": [31, 34], "min_severity": "MAJOR", "anchor": "state.save_cursor(page.next_cursor)",
     "keywords": r"cursor|before (the )?(items|upsert|process)|crash|lost|loss|at.most.once|skip"}],
  [{"lines": [12, 13], "keywords": r"backoff|jitter|overflow|2 \\*\\*|exponent"}])

R["v03"] = ("src/api/orders.py", r'''from fastapi import APIRouter, Depends, HTTPException, Query

from .auth import current_user, require_admin
from .db import get_conn

router = APIRouter()

SORTABLE = {"created_at", "total", "status"}


@router.get("/orders")
def list_orders(sort: str = Query("created_at"), q: str = "", user=Depends(current_user), conn=Depends(get_conn)):
    pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    sql = f"SELECT id, status, total FROM orders WHERE tenant_id = %s AND note LIKE %s ESCAPE '\\' ORDER BY {sort}"
    return conn.execute(sql, (user.tenant_id, pattern)).fetchall()


@router.get("/orders/{order_id}")
def get_order(order_id: int, user=Depends(current_user), conn=Depends(get_conn)):
    row = conn.execute("SELECT * FROM orders WHERE id = %s AND tenant_id = %s", (order_id, user.tenant_id)).fetchone()
    if row is None:
        raise HTTPException(404)
    return row


@router.post("/orders/{order_id}/cancel")
def cancel_order(order_id: int, user=Depends(current_user), conn=Depends(get_conn)):
    cur = conn.execute("UPDATE orders SET status = 'cancelled' WHERE id = %s AND status = 'open'", (order_id,))
    if cur.rowcount == 0:
        raise HTTPException(404)
    return {"cancelled": order_id}


@router.delete("/admin/orders/{order_id}")
def purge_order(order_id: int, admin=Depends(require_admin), conn=Depends(get_conn)):
    conn.execute("DELETE FROM orders WHERE id = %s", (order_id,))
    return {"purged": order_id}
''', [
    {"id": "D1", "lines": [12, 14], "min_severity": "BLOCKER", "anchor": "ORDER BY {sort}",
     "keywords": r"inject|ORDER BY|SORTABLE|allow.?list|white.?list|sort (param|column|value)|f-string|interpolat"},
    {"id": "D2", "lines": [27, 28], "min_severity": "BLOCKER", "anchor": "UPDATE orders SET status = 'cancelled'",
     "keywords": r"tenant|IDOR|authori[sz]|any (user|order)|other (tenant|user|customer)|cross|ownership"}],
  [{"lines": [13, 13], "keywords": r"LIKE|wildcard|escap"}])

R["v04"] = ("src/reports/revenue.py", '''REVENUE_BY_CUSTOMER = """
SELECT c.id,
       c.name,
       COALESCE(SUM(oi.quantity * oi.unit_price), 0) AS gross,
       COALESCE(SUM(p.amount), 0)                    AS paid
FROM customers c
LEFT JOIN orders o       ON o.customer_id = c.id
LEFT JOIN order_items oi ON oi.order_id = o.id
LEFT JOIN payments p     ON p.order_id = o.id
WHERE p.status = 'settled'
GROUP BY c.id, c.name
"""

CUSTOMERS_WITHOUT_ORDERS = """
SELECT c.id
FROM customers c
WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.id)
"""


def revenue_by_customer(conn):
    return conn.execute(REVENUE_BY_CUSTOMER).fetchall()


def customers_without_orders(conn):
    return [r[0] for r in conn.execute(CUSTOMERS_WITHOUT_ORDERS).fetchall()]
''', [
    {"id": "D1", "lines": [9, 10], "min_severity": "MAJOR", "anchor": "WHERE p.status = 'settled'",
     "keywords": r"inner join|LEFT JOIN|WHERE|ON clause|drops?|exclud|filters? out|without (a )?(settled )?payment|no payment"},
    {"id": "D2", "lines": [4, 9], "min_severity": "MAJOR", "anchor": "COALESCE(SUM(oi.quantity",
     "keywords": r"fan.?out|double|multipl|cartesian|duplicat|inflat|over.?count|row explosion|count(ed)? (twice|more than once)"}],
  [{"lines": [15, 17], "keywords": r"NOT EXISTS|NULL|NOT IN"}])

R["v05"] = ("src/notify/digest.py", '''import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)


def due_users(users, now=None):
    now = now or datetime.now(timezone.utc)
    return [u for u in users if u["next_digest_at"] <= now]


def send_digests(users, mailer, store, now=None):
    sent, failed = 0, []
    for user in due_users(users, now):
        try:
            mailer.send(user["email"], build_digest(user))
        except mailer.TemporaryError as exc:
            log.warning("temporary failure for %s: %s", user["id"], exc)
            failed.append(user["id"])
            continue
        except Exception:
            log.debug("digest skipped for %s", user["id"])
        user["next_digest_at"] = user["next_digest_at"] + timedelta(days=1)
        store.save(user)
        sent += 1
    return {"sent": sent, "failed": failed}


def build_digest(user):
    items = user.get("items") or []
    lines = [f"- {i['title']}" for i in items[:20]]
    return "Your daily digest:\\n" + "\\n".join(lines)
''', [
    {"id": "D1", "lines": [21, 25], "min_severity": "MAJOR", "anchor": "except Exception:",
     "keywords": r"debug|swallow|falls? through|counted as sent|sent \\+= 1|false success|silently|reported as sent|still (advanced|counted|saved)"},
    {"id": "D2", "lines": [23, 23], "min_severity": "MAJOR", "anchor": "user[\"next_digest_at\"] + timedelta(days=1)",
     "keywords": r"catch.?up|burst|backlog|from now|still (be )?due|repeated|multiple digests|every run|downtime|past|stale|drift|instead of now"}],
  [{"lines": [7, 9], "keywords": r"timezone|naive|utc|tz"}])

key = {"reviewer": {}, "tester": {}}
for code, (rel, src, defects, traps) in R.items():
    p = B2 / code / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(src, encoding="utf-8", newline="\n")
    lines = src.split("\n")
    for d in defects:  # self-check: anchor must sit inside the declared line range
        hit = [i + 1 for i, l in enumerate(lines) if d["anchor"] in l]
        assert hit and d["lines"][0] <= hit[0] <= d["lines"][1], (code, d["id"], hit, d["lines"])
    key["reviewer"][code] = {"file": rel, "defects": defects, "traps": traps}

# ---------------- tester cases ----------------
T = {}
T["t01"] = ("split.py", '''# split_amount(total_cents, parts, weights=None) -> list[int]

1. Returns exactly `parts` integers whose sum equals `total_cents`.
2. Without weights: the amounts differ by at most 1 cent and the larger amounts come first.
3. With weights (a list of `parts` non-negative integers with a positive sum): each part first gets
   floor(total_cents * weight / sum(weights)); the leftover cents are then handed out one at a time
   to the parts with the largest weights (ties: lower index first).
4. `total_cents` must be >= 0 and `parts` >= 1, otherwise ValueError.
5. Weights of the wrong length, any negative weight, or all-zero weights raise ValueError.
''', '''def split_amount(total_cents, parts, weights=None):
    if total_cents < 0 or parts < 1:
        raise ValueError("bad arguments")
    if weights is None:
        base, extra = divmod(total_cents, parts)
        return [base + 1 if i < extra else base for i in range(parts)]
    if len(weights) != parts or any(w < 0 for w in weights) or sum(weights) == 0:
        raise ValueError("bad weights")
    total_w = sum(weights)
    shares = [total_cents * w // total_w for w in weights]
    leftover = total_cents - sum(shares)
    order = sorted(range(parts), key=lambda i: (-weights[i], i))
    for i in order[:leftover]:
        shares[i] += 1
    return shares
''', {"B1": ("leftover cents dropped with weights", "    for i in order[:leftover]:\n        shares[i] += 1\n", "")})

T["t02"] = ("duration.py", '''# parse_duration(text) -> int (seconds)

1. Accepts tokens `<digits>h`, `<digits>m`, `<digits>s` in exactly that order; each token is optional
   but at least one must be present. Example: "1h30m" -> 5400, "45s" -> 45, "2h 5s" -> 7205.
2. Spaces between tokens and around the text are allowed; units are case-insensitive ("1H" -> 3600).
3. Raises ValueError for: an empty or whitespace-only string; unknown units ("5d"); tokens out of
   order ("5m1h"); a repeated unit ("1h2h"); signs or non-digits in a number ("-5m", "1.5h").
''', '''import re

_TOKEN = re.compile(r"(\\d+)\\s*([hms])", re.I)
_ORDER = "hms"


def parse_duration(text):
    s = text.strip().lower()
    if not s:
        raise ValueError("empty duration")
    pos, last, total = 0, -1, 0
    for m in _TOKEN.finditer(s):
        if s[pos:m.start()].strip():
            raise ValueError("invalid duration")
        unit = _ORDER.index(m.group(2).lower())
        if unit <= last:
            raise ValueError("units out of order or repeated")
        last = unit
        total += int(m.group(1)) * (3600, 60, 1)[unit]
        pos = m.end()
    if s[pos:].strip() or last == -1:
        raise ValueError("invalid duration")
    return total
''', {"B1": ("empty string returns 0", "    if not s:\n        raise ValueError(\"empty duration\")\n", "    if not s:\n        return 0\n"),
      "B2": ("out-of-order units accepted", "        if unit <= last:\n", "        if unit == last:\n")})

T["t03"] = ("discount.py", '''# apply_discount(price_cents, code, today, codes) -> int

`codes` maps a code to {"percent": int, "valid_from": date, "valid_to": date}.

1. Codes are matched case-insensitively ("save10" finds "SAVE10").
2. A code is valid when valid_from <= today <= valid_to (both ends inclusive).
3. The percent is capped at 50.
4. The result is price_cents * (100 - percent) / 100 rounded half-up to a whole cent, never below 0.
5. An unknown code, or a code outside its validity window, raises ValueError("invalid code").
''', '''def apply_discount(price_cents, code, today, codes):
    entry = None
    for name, value in codes.items():
        if name.lower() == code.lower():
            entry = value
            break
    if entry is None or not (entry["valid_from"] <= today <= entry["valid_to"]):
        raise ValueError("invalid code")
    percent = min(entry["percent"], 50)
    discounted = (price_cents * (100 - percent) * 2 + 100) // 200
    return max(discounted, 0)
''', {"B1": ("valid_to treated as exclusive", "today <= entry[\"valid_to\"]", "today < entry[\"valid_to\"]")})

for code, (mod, spec, impl, bugs) in T.items():
    buggy = impl
    for _, (desc, old, new) in bugs.items():
        assert old in buggy, (code, desc)
        buggy = buggy.replace(old, new)
    for cond in ("author", "indep"):
        d = T2 / f"{code}_{cond}"
        (d / "tests").mkdir(parents=True, exist_ok=True)
        (d / "SPEC.md").write_text(spec, encoding="utf-8", newline="\n")
        (d / mod).write_text(buggy, encoding="utf-8", newline="\n")   # agents see the BUGGY implementation
    h = HID / code
    h.mkdir(parents=True, exist_ok=True)
    (h / "correct.py").write_text(impl, encoding="utf-8", newline="\n")
    (h / "buggy_all.py").write_text(buggy, encoding="utf-8", newline="\n")
    for bid, (desc, old, new) in bugs.items():
        assert old in impl
        (h / f"buggy_{bid}.py").write_text(impl.replace(old, new), encoding="utf-8", newline="\n")
    key["tester"][code] = {"module": mod, "bugs": {b: v[0] for b, v in bugs.items()}}

(ROOT / "bench2_key.json").write_text(json.dumps(key, indent=1), encoding="utf-8")
print("ok", sorted(key["reviewer"]), sorted(key["tester"]))
