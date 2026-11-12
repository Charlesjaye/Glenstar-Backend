from datetime import date

import quarters as q


def test_parse_formats():
    assert q.parse('Q3 2026') == 'Q3 2026'
    assert q.parse('q3-2026') == 'Q3 2026'
    assert q.parse('3Q26') == 'Q3 2026'
    assert q.parse('3Q 2026') == 'Q3 2026'
    assert q.parse("Q3 '26") == 'Q3 2026'
    assert q.parse('2026 Q3') == 'Q3 2026'
    assert q.parse('Third Quarter 2026') == 'Q3 2026'
    assert q.parse('MarketBeat Dallas Industrial Q1 2027') == 'Q1 2027'
    assert q.parse('no quarter here 2026') is None
    assert q.parse(None) is None


def test_index_round_trip_and_shift():
    assert q.from_index(q.index('Q4 2025')) == 'Q4 2025'
    assert q.shift('Q4 2025', 1) == 'Q1 2026'
    assert q.shift('Q1 2026', -1) == 'Q4 2025'
    assert q.index('Q3 2026') > q.index('Q1 2026')


def test_latest_published_follows_the_calendar():
    # Reports for a quarter count as published three weeks after it ends.
    assert q.latest_published(date(2026, 11, 11)) == 'Q3 2026'
    assert q.latest_published(date(2026, 10, 5)) == 'Q2 2026'    # Q3 ended 5 days ago
    assert q.latest_published(date(2026, 10, 21)) == 'Q3 2026'
    assert q.latest_published(date(2026, 4, 30)) == 'Q1 2026'
    assert q.latest_published(date(2026, 7, 25)) == 'Q2 2026'
    assert q.latest_published(date(2027, 1, 10)) == 'Q3 2026'
    assert q.latest_published(date(2027, 1, 25)) == 'Q4 2026'
    assert q.latest_published(date(2027, 3, 31)) == 'Q4 2026'


def test_never_stuck_on_q1():
    # Regression: the old logic returned Q1 for every month from April onward.
    seen = {q.latest_published(date(2026, m, 28)) for m in range(1, 13)}
    assert seen == {'Q4 2025', 'Q1 2026', 'Q2 2026', 'Q3 2026'}
