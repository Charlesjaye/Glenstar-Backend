"""Quarter arithmetic. A quarter is represented as the string "Q3 2026" and,
for sorting/comparison, as an integer index (year * 4 + quarter - 1)."""
import re
from datetime import date, timedelta

# Broker quarterly reports typically start appearing about three weeks after
# quarter end. Until then, the previous quarter is still "the latest published".
PUBLICATION_LAG_DAYS = 21

_Q_RE = re.compile(r'\bQ\s*([1-4])\s*[-/ ,\']*\s*(20\d{2}|\d{2})\b', re.I)
_Q_RE_YEAR_FIRST = re.compile(r'\b(20\d{2})\s*[-/ ]?\s*Q\s*([1-4])\b', re.I)
_ORDINALS = {'first': 1, '1st': 1, 'second': 2, '2nd': 2, 'third': 3, '3rd': 3, 'fourth': 4, '4th': 4}
_Q_RE_WORDS = re.compile(r'\b(first|second|third|fourth|1st|2nd|3rd|4th)\s+quarter(?:\s+of)?\s+(20\d{2})\b', re.I)


def make(year, q):
    return f"Q{q} {year}"


def parse(text):
    """Parse 'Q3 2026', '3Q26', '2026 Q3', 'Third Quarter 2026' → 'Q3 2026' (or None)."""
    if not text:
        return None
    s = str(text).strip()
    m = _Q_RE.search(s)
    if m:
        year = int(m.group(2))
        return make(year + 2000 if year < 100 else year, int(m.group(1)))
    m = re.search(r'\b([1-4])\s*Q\s*[-/ \']*\s*(20\d{2}|\d{2})\b', s, re.I)
    if m:
        year = int(m.group(2))
        return make(year + 2000 if year < 100 else year, int(m.group(1)))
    m = _Q_RE_YEAR_FIRST.search(s)
    if m:
        return make(int(m.group(1)), int(m.group(2)))
    m = _Q_RE_WORDS.search(s)
    if m:
        return make(int(m.group(2)), _ORDINALS[m.group(1).lower()])
    return None


def index(quarter):
    """'Q3 2026' → sortable int. Returns None for unparseable input."""
    q = parse(quarter)
    if not q:
        return None
    return int(q[3:]) * 4 + int(q[1]) - 1


def from_index(idx):
    return make(idx // 4, idx % 4 + 1)


def shift(quarter, n):
    return from_index(index(quarter) + n)


def calendar_quarter(today=None):
    """The quarter the given date falls in."""
    today = today or date.today()
    return make(today.year, (today.month - 1) // 3 + 1)


def quarter_end(quarter):
    q, year = int(quarter[1]), int(quarter[3:])
    month = q * 3
    first_next = date(year + (month // 12), month % 12 + 1, 1)
    return first_next - timedelta(days=1)


def latest_published(today=None):
    """The most recent quarter whose broker reports should be available by `today`:
    the latest quarter that ended at least PUBLICATION_LAG_DAYS ago."""
    today = today or date.today()
    candidate = shift(calendar_quarter(today), -1)
    if (today - quarter_end(candidate)).days < PUBLICATION_LAG_DAYS:
        candidate = shift(candidate, -1)
    return candidate
