import verification as v

PAGES = [
    'JLL Research\nDallas-Fort Worth Industrial Insight\nQ3 2026',
    'Total vacancy rate ended the quarter at 7.2%, down 20 basis points.\n'
    'Net absorption totaled 6,412,300 SF in the third quarter.\n'
    'Year-to-date net absorption reached 18.4 million SF.\n'
    'Average asking rent was $9.85 per SF per year NNN.\n'
    'Market   Inventory   Vacancy   Net Abs\nDallas-Fort Worth 1,023,456 17.2% (350,000)',
]


def run(metric, unit, value, printed, quote, page=2, has_text=True):
    checks, evidence, resolved = v.check_value(
        {'metric': metric, 'unit': unit, 'value': value, 'printed': printed, 'quote': quote, 'page': page},
        PAGES, has_text)
    return checks, evidence, resolved, v.decide_status(checks, evidence, 'correct')


def failed(checks):
    return [c['check'] for c in checks if not c['ok']]


def test_exact_quote_verifies():
    checks, evidence, page, status = run('vacancy_rate', 'percent', 7.2, '7.2%',
                                         'Total vacancy rate ended the quarter at 7.2%')
    assert evidence == 'quote' and status == 'verified' and not failed(checks)


def test_quote_matching_ignores_pdf_spacing_and_dash_styles():
    checks, evidence, _, status = run('asking_rent', 'usd_psf', 9.85, '$9.85',
                                      'Average  asking rent was $9.85\nper SF per year NNN.')
    assert evidence == 'quote' and status == 'verified'


def test_invented_number_is_held():
    checks, evidence, _, status = run('vacancy_rate', 'percent', 6.9, '6.9%', 'Total vacancy rate was 6.9%')
    assert evidence is None and status == 'needs_review'
    assert 'found in document text' in failed(checks)


def test_number_must_be_standalone_not_part_of_another():
    # 7.2 must not be "found" inside 17.2%
    pages = ['x', 'Dallas-Fort Worth 1,023,456 17.2% (350,000)']
    checks, evidence, _ = v.check_value(
        {'metric': 'vacancy_rate', 'unit': 'percent', 'value': 7.2, 'printed': '7.2%',
         'quote': 'Vacancy 7.2%', 'page': 2}, pages, True)
    assert evidence is None


def test_stored_value_must_equal_printed_figure():
    checks, _, _, status = run('vacancy_rate', 'percent', 7.5, '7.2%',
                               'Total vacancy rate ended the quarter at 7.2%')
    assert status == 'needs_review' and 'matches printed figure' in failed(checks)


def test_square_feet_scaling():
    _, _, _, status = run('net_absorption_ytd', 'sf', 18_400_000, '18.4 million SF',
                          'Year-to-date net absorption reached 18.4 million SF.')
    assert status == 'verified'
    checks, _, _, status = run('net_absorption_ytd', 'sf', 18.4, '18.4 million SF',
                               'Year-to-date net absorption reached 18.4 million SF.')
    assert status == 'needs_review' and 'matches printed figure' in failed(checks)
    _, _, _, status = run('net_absorption_qtr', 'sf', 6_412_300, '6,412,300 SF',
                          'Net absorption totaled 6,412,300 SF in the third quarter.')
    assert status == 'verified'


def test_parentheses_mean_negative():
    quote = 'Dallas-Fort Worth 1,023,456 17.2% (350,000)'
    _, _, _, status = run('net_absorption_qtr', 'sf', -350_000, '(350,000)', quote)
    assert status == 'verified'
    checks, _, _, status = run('net_absorption_qtr', 'sf', 350_000, '(350,000)', quote)
    assert status == 'needs_review' and 'sign' in failed(checks)


def test_negative_without_evidence_is_held():
    checks, _, _, status = run('net_absorption_qtr', 'sf', -6_412_300, '6,412,300 SF',
                               'Net absorption totaled 6,412,300 SF in the third quarter.')
    assert status == 'needs_review' and 'sign' in failed(checks)


def test_implausible_value_is_held():
    pages = ['Vacancy rate was 72%']
    checks, evidence, _ = v.check_value({'metric': 'vacancy_rate', 'unit': 'percent', 'value': 72,
                                         'printed': '72%', 'quote': 'Vacancy rate was 72%', 'page': 1}, pages, True)
    assert 'plausible range' in failed(checks)
    assert v.decide_status(checks, evidence, 'correct') == 'needs_review'


def test_wrong_page_is_corrected_when_quote_is_elsewhere():
    _, evidence, page, status = run('vacancy_rate', 'percent', 7.2, '7.2%',
                                    'Total vacancy rate ended the quarter at 7.2%', page=1)
    assert evidence == 'quote' and page == 2 and status == 'verified'


def test_table_figure_needs_the_audit_to_agree():
    args = {'metric': 'inventory', 'unit': 'sf', 'value': 1_023_456_000, 'printed': '1,023,456',
            'quote': 'Dallas-Fort Worth | 1,023,456 | 17.2%', 'page': 2}
    checks, evidence, _ = v.check_value(args, PAGES, True)
    assert evidence == 'number'                       # figure is on the page; quote is not word-for-word
    assert v.decide_status(checks, evidence, 'correct') == 'verified'
    assert v.decide_status(checks, evidence, None) == 'needs_review'
    assert v.decide_status(checks, evidence, 'incorrect') == 'needs_review'


def test_scanned_pdf_is_never_auto_verified():
    checks, evidence, _, status = run('vacancy_rate', 'percent', 7.2, '7.2%', 'Vacancy 7.2%', has_text=False)
    assert evidence is None and status == 'needs_review'


def test_audit_disagreement_overrides_text_match():
    checks, evidence, _, _ = run('vacancy_rate', 'percent', 7.2, '7.2%',
                                 'Total vacancy rate ended the quarter at 7.2%')
    assert v.decide_status(checks, evidence, 'incorrect') == 'needs_review'
    assert v.decide_status(checks, evidence, 'cannot_find') == 'needs_review'
    assert v.decide_status(checks, evidence, None) == 'verified'      # exact quote stands on its own


def test_cross_check_flags_conflicts_and_swaps():
    rows = [
        {'market': 'A', 'metric': 'vacancy_rate', 'segment': '', 'value': 9.0, 'checks': []},
        {'market': 'A', 'metric': 'availability_rate', 'segment': '', 'value': 7.0, 'checks': []},
        {'market': 'B', 'metric': 'asking_rent', 'segment': '', 'value': 9.1, 'checks': []},
        {'market': 'B', 'metric': 'asking_rent', 'segment': '', 'value': 9.9, 'checks': []},
        {'market': 'C', 'metric': 'vacancy_rate', 'segment': '', 'value': 5.0, 'checks': []},
    ]
    v.cross_check(rows)
    assert all(r['checks'] for r in rows[:4]) and not rows[4]['checks']


def test_parse_printed():
    assert v.parse_printed('(1,234,567)')['negative'] is True
    assert v.parse_printed('-0.8%')['negative'] is True
    assert v.parse_printed('−0.8%')['negative'] is True      # unicode minus
    assert v.parse_printed('$9.85')['number'] == 9.85
    assert v.parse_printed('24.2 MSF')['scale'] == 1e6
    assert v.parse_printed('1.2 million SF')['scale'] == 1e6
    assert v.parse_printed('50 bps')['scale'] == 0.01
    assert v.parse_printed('n/a') is None
