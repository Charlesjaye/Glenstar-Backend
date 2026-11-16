"""Building-size bands.

Brokers break markets down by size using their own ranges ("100,000-249,999 SF",
"Under 100K", "500K+"...). This module reads the range out of a label and maps
it onto Glenstar's standard bands so different brokers line up on one scale.

Nothing is interpolated: a broker band is shown under every standard band it
overlaps, marked as an exact match, a narrower slice, or a wider range."""
import math
import re

# Glenstar's standard size bands (square feet). Upper bound is exclusive.
STANDARD_BANDS = [
    {'key': 'b1', 'label': 'Under 50K SF',  'short': '<50K',      'note': 'Small-bay',   'min': 0,       'max': 50_000},
    {'key': 'b2', 'label': '50K–100K SF',   'short': '50–100K',   'note': 'Shallow-bay', 'min': 50_000,  'max': 100_000},
    {'key': 'b3', 'label': '100K–250K SF',  'short': '100–250K',  'note': 'Mid-bay',     'min': 100_000, 'max': 250_000},
    {'key': 'b4', 'label': '250K–500K SF',  'short': '250–500K',  'note': 'Cross-dock',  'min': 250_000, 'max': 500_000},
    {'key': 'b5', 'label': '500K–750K SF',  'short': '500–750K',  'note': 'Big-box',     'min': 500_000, 'max': 750_000},
    {'key': 'b6', 'label': '750K SF and up', 'short': '750K+',    'note': 'Mega-box',    'min': 750_000, 'max': math.inf},
]
BAND_BY_KEY = {b['key']: b for b in STANDARD_BANDS}

_NUM = re.compile(r'(\d[\d,]*(?:\.\d+)?)\s*(million|mil\b|mm\b|m\b|msf|thousand|k\b|ksf)?', re.I)
_BELOW = re.compile(r'(under|less than|below|up to|smaller than|<|≤)', re.I)
_ABOVE = re.compile(r'(\+|and above|and over|and up|and greater|and larger|or more|or larger|over|above|greater than|'
                    r'larger than|more than|plus|>|≥)', re.I)
_TYPE = re.compile(r'(warehouse|distribution|flex|manufactur|r&d|bulk|logistics|light industrial|general industrial|'
                   r'class [abc]\b|cold storage|truck terminal|showroom|data cent|incubator|special purpose)', re.I)


def _to_sf(number, suffix):
    value = float(number.replace(',', ''))
    s = (suffix or '').lower()
    if s in ('million', 'mil', 'mm', 'm', 'msf'):
        return value * 1_000_000
    if s in ('thousand', 'k', 'ksf'):
        return value * 1_000
    return value


def _tidy(value):
    """249,999 → 250,000 (brokers write inclusive upper bounds)."""
    rounded = round(value + 1)
    return float(rounded) if value >= 999 and str(int(value)).endswith('999') else float(value)


def parse_size_range(label):
    """Read a size range from a label. Returns (min_sf, max_sf) with max = inf for
    open-ended bands, or None when the label is not a size band."""
    text = (label or '').replace('–', '-').replace('—', '-').replace('−', '-')
    text = re.sub(r'\bsq\.?\s*ft\.?|\bsquare feet\b|\bsf\b', ' ', text, flags=re.I)
    text = re.sub(r'\b[1-4]?Q[1-4]?\b|\b(?:19|20)\d{2}\b(?!\s*(?:k|m)\b)', ' ', text, flags=re.I)   # quarters and years are not sizes
    found = [(m.group(1), m.group(2)) for m in _NUM.finditer(text)]
    found = [(n, s) for n, s in found if re.search(r'\d', n)]
    if not found:
        return None
    # "100-250K": the first number takes the second's unit when it has none.
    if len(found) >= 2 and not found[0][1] and found[1][1]:
        first = _to_sf(found[0][0], None)
        if first < 1000:
            found[0] = (found[0][0], found[1][1])
    values = [_to_sf(n, s) for n, s in found[:2]]
    # Bare small numbers ("100-250") are almost always thousands in size tables.
    if all(v < 5000 for v in values) and not any(s for _, s in found[:2]):
        values = [v * 1000 for v in values]
    if len(values) == 1:
        v = values[0]
        if v < 1000:
            return None
        if _BELOW.search(text):
            return 0.0, _tidy(v)
        if _ABOVE.search(text):
            return _tidy(v), math.inf
        return None
    lo, hi = sorted(values)
    if hi < 1000 or lo == hi:
        return None
    return _tidy(lo), _tidy(hi)


def classify(label):
    """→ dict(kind, size_min, size_max). kind is 'size', 'type' (product type) or 'other'."""
    rng = parse_size_range(label)
    if rng:
        return {'kind': 'size', 'size_min': rng[0], 'size_max': None if math.isinf(rng[1]) else rng[1]}
    return {'kind': 'type' if _TYPE.search(label or '') else 'other', 'size_min': None, 'size_max': None}


def bands_for(size_min, size_max):
    """Standard bands a broker range overlaps, and how well it fits.
    fit: 'exact' (same range), 'within' (a narrower slice of one band), 'spans' (wider than one band)."""
    if size_min is None:
        return [], None
    hi = math.inf if size_max is None else size_max
    keys = [b['key'] for b in STANDARD_BANDS if size_min < b['max'] and hi > b['min']]
    if not keys:
        return [], None
    if len(keys) == 1:
        band = BAND_BY_KEY[keys[0]]
        fit = 'exact' if size_min == band['min'] and hi == band['max'] else 'within'
    else:
        fit = 'spans'
    return keys, fit


def format_range(size_min, size_max):
    def f(v):
        if v >= 1_000_000:
            return f'{v / 1_000_000:g}M'
        return f'{v / 1000:g}K'
    if size_min is None:
        return ''
    if size_max is None:
        return f'{f(size_min)}+ SF'
    if size_min == 0:
        return f'Under {f(size_max)} SF'
    return f'{f(size_min)}–{f(size_max)} SF'
