"""Independent checks on every value the AI reads from a report.

The AI is never trusted on its own. For each value it must return the figure
exactly as printed, a verbatim quote, and a page number. This module then
checks, with plain code:

  1. the quote really appears in the document's text (on the cited page, or elsewhere);
  2. the printed figure appears inside that quote;
  3. the stored number equals the printed figure (after unit scaling, e.g. "1.2 MSF" = 1,200,000 SF);
  4. the sign is right (parentheses / minus sign / "negative");
  5. the value is physically plausible for that metric;
  6. it does not contradict another value from the same document.

A value is published automatically only if these pass (and the second-pass AI
audit, when it ran, agrees). Anything else is held as `needs_review` and never
shown on the site until a person confirms it against the PDF.
"""
import re
import unicodedata

from catalog import METRIC_BY_KEY

_DASHES = '‐‑‒–—―−﹘﹣－'
_NUM = re.compile(r'\d[\d,]*(?:\.\d+)?|\.\d+')


def norm(text):
    """Normalise text for comparison: unicode forms, dashes, quotes, case, whitespace."""
    s = unicodedata.normalize('NFKC', text or '')
    for ch in _DASHES:
        s = s.replace(ch, '-')
    s = s.replace('‘', "'").replace('’', "'").replace('“', '"').replace('”', '"')
    s = s.replace('­', '')
    return ' '.join(s.lower().split())


def squash(text):
    """Normalised text with all whitespace removed (PDF text often has odd spacing)."""
    return re.sub(r'\s+', '', norm(text))


def find_quote(pages, quote, cited_page):
    """Locate `quote` in the document text. Returns the 1-based page it is on, or None.
    The cited page is tried first, then every other page."""
    q = squash(quote)
    if len(q) < 6:
        return None
    order = []
    if cited_page and 1 <= cited_page <= len(pages):
        order.append(cited_page)
    order += [i for i in range(1, len(pages) + 1) if i != cited_page]
    for p in order:
        if q in squash(pages[p - 1]):
            return p
    return None


def number_token_pattern(token):
    """Regex matching `token` as a standalone number (not part of a longer one)."""
    return re.compile(r'(?<![\d.,])' + re.escape(token) + r'(?![\d]|[.,]\d)')


def number_on_page(pages, token, page):
    if not token or not page or not (1 <= page <= len(pages)):
        return False
    return bool(number_token_pattern(token).search(norm(pages[page - 1])))


def parse_printed(printed):
    """Parse a figure as printed, e.g. '(1,234,567)', '-0.8%', '$9.85', '24.2 MSF', '1.2 million SF'.
    Returns dict(number, token, negative, scale) or None."""
    s = norm(printed)
    m = _NUM.search(s)
    if not m:
        return None
    token = m.group(0)
    try:
        number = float(token.replace(',', ''))
    except ValueError:
        return None
    before, after = s[:m.start()], s[m.end():]
    negative = bool(
        re.search(r'-\s*\$?\s*$', before)
        or (re.search(r'\(\s*\$?\s*$', before) and re.match(r'\s*[a-z%$/ ]*\)', after))
        or re.search(r'\bnegative\b', before)
    )
    scale = None
    a = after.strip()
    if re.match(r'(billion|bn\b|b\s*sf|bsf)', a):
        scale = 1e9
    elif re.match(r'(million|mil\b|mm\b|msf|m\s*sf|m\b)', a):
        scale = 1e6
    elif re.match(r'(thousand|k\b|ksf|k\s*sf)', a):
        scale = 1e3
    elif re.match(r'(bps|bp\b|basis)', a):
        scale = 0.01
    years = bool(re.match(r'(-?\s*)?(years?|yrs?)\b', a))
    return {'number': number, 'token': token, 'negative': negative, 'scale': scale, 'years': years}


def _close(a, b):
    return abs(a - b) <= max(1e-9, 1e-6 * max(abs(a), abs(b)))


def value_matches_printed(unit, value, parsed):
    """Does the stored value equal the printed figure, allowing only legitimate unit scaling?"""
    n = parsed['number']
    if unit == 'sf':
        candidates = [n * parsed['scale']] if parsed['scale'] in (1e3, 1e6, 1e9) else [n, n * 1e3, n * 1e6]
    elif unit == 'percent':
        candidates = [n * 0.01] if parsed['scale'] == 0.01 else [n]
    elif unit == 'months':
        candidates = [n * 12] if parsed.get('years') else [n]
    else:
        candidates = [n]
    return any(_close(abs(value), c) for c in candidates)


def check_value(v, pages, has_text):
    """Run the automatic checks on one extracted value.

    `v` is a dict with metric, unit, value, printed, quote, page.
    Returns (checks, evidence, resolved_page) where evidence is
    'quote' (verbatim quote found), 'number' (figure found on the cited page) or None.
    """
    checks = []
    metric = METRIC_BY_KEY.get(v.get('metric'))
    value = v.get('value')
    parsed = parse_printed(v.get('printed'))
    page = v.get('page')
    evidence = None

    def add(name, ok, detail):
        checks.append({'check': name, 'ok': bool(ok), 'detail': detail})

    if metric is None or value is None:
        add('recognised metric', False, 'Unknown metric or missing value.')
        return checks, None, page

    # 1–2. Evidence in the document text
    if not has_text:
        add('found in document text', False,
            'This PDF has no text layer (it is a scan/image), so the figure cannot be checked automatically.')
    elif not parsed:
        add('found in document text', False, 'No number could be read from the printed figure.')
    else:
        found_page = find_quote(pages, v.get('quote', ''), page)
        token_in_quote = bool(number_token_pattern(parsed['token']).search(norm(v.get('quote', ''))))
        if found_page and token_in_quote:
            evidence = 'quote'
            note = '' if found_page == page else f' (cited page {page}; actually on page {found_page})'
            add('found in document text', True, f'Quote found word-for-word on page {found_page}{note}.')
            page = found_page
        elif number_on_page(pages, parsed['token'], page):
            evidence = 'number'
            add('found in document text', True,
                f'The figure {parsed["token"]} appears on page {page}; the surrounding quote could not be matched '
                'word-for-word (common with tables).')
        else:
            add('found in document text', False,
                f'Neither the quote nor the figure {parsed["token"]} could be found on page {page}.')

    # 3–4. Stored value vs printed figure
    if parsed:
        if value_matches_printed(metric['unit'], value, parsed):
            add('matches printed figure', True, f'Stored value equals the printed figure "{v.get("printed")}".')
        else:
            add('matches printed figure', False,
                f'Stored value {value:g} does not equal the printed figure "{v.get("printed")}".')
        quote_n = norm(v.get('quote', ''))
        tok = re.escape(parsed['token'])
        negative_indicated = (
            parsed['negative']
            or bool(re.search(r'-\s*\$?\s*' + tok, quote_n))
            or bool(re.search(r'\(\s*\$?\s*' + tok + r'[^)]{0,12}\)', quote_n))
            or bool(re.search(r'\bnegative\b[^.]{0,40}' + tok, quote_n))
        )
        if value < 0 and not negative_indicated:
            add('sign', False, 'Stored as negative but the printed figure shows no minus sign or parentheses.')
        elif value > 0 and negative_indicated:
            add('sign', False, 'Printed as negative (minus sign or parentheses) but stored as positive.')
    else:
        add('matches printed figure', False, 'No printed figure supplied.')

    # 5. Plausibility
    lo, hi = metric['range']
    if not (lo <= value <= hi):
        add('plausible range', False, f'{value:g} is outside the plausible range for {metric["label"].lower()}.')

    return checks, evidence, page


def cross_check(values):
    """Checks across values from the same document. `values` are dicts with
    market, metric, segment, value and a `checks` list; failures are appended."""
    by_key = {}
    for v in values:
        by_key.setdefault((v['market'], v['metric'], v.get('segment') or '', v.get('period_offset') or 0), []).append(v)
    for _key, group in by_key.items():
        distinct = {round(g['value'], 6) for g in group if g.get('value') is not None}
        if len(distinct) > 1:
            for g in group:
                g['checks'].append({'check': 'consistent within report', 'ok': False,
                                    'detail': 'The report was read as giving more than one value for this metric.'})
    totals = {(v['market'], v['metric']): v for v in values
              if not (v.get('segment') or '') and not (v.get('period_offset') or 0)}
    for (market, metric), vac in list(totals.items()):
        if metric != 'vacancy_rate':
            continue
        avail = totals.get((market, 'availability_rate'))
        if avail and vac.get('value') is not None and avail.get('value') is not None \
                and vac['value'] > avail['value'] + 1e-9:
            for g in (vac, avail):
                g['checks'].append({'check': 'consistent within report', 'ok': False,
                                    'detail': 'Vacancy is higher than availability, which is not normally possible — '
                                              'the two may have been swapped.'})


def decide_status(checks, evidence, audit_verdict):
    """Final automatic decision for one value.
    audit_verdict: 'correct' | 'incorrect' | 'cannot_find' | None (audit did not run)."""
    if any(not c['ok'] for c in checks):
        return 'needs_review'
    if audit_verdict in ('incorrect', 'cannot_find'):
        return 'needs_review'
    if evidence == 'quote':
        return 'verified'
    if evidence == 'number' and audit_verdict == 'correct':
        return 'verified'
    return 'needs_review'
