"""Size bands, direction-of-travel indicators and the underwriting sheet, end to end."""
import data
import ingest
from conftest import dfw_extraction, make_pdf
from models import MetricValue, db

PAGES = [
    ['JLL Research', 'Dallas-Fort Worth Industrial Insight', 'Q3 2026',
     'Demand from logistics and manufacturing occupiers stayed healthy through the third quarter while',
     'construction starts slowed across the metroplex, pulling the development pipeline lower.'],
    ['Market statistics            Q3 2026     Q2 2026     Q3 2025',
     'Total vacancy rate ended the quarter at 7.2%, down 20 basis points.',
     'Vacancy rate prior quarter: 7.4%',
     'Vacancy rate one year ago: 8.1%',
     'Average asking rent was $9.85 per SF per year NNN.',
     'Asking rent one year ago: $9.40',
     'Net absorption totaled 6,412,300 SF in the third quarter.',
     'Deliveries: 5,210,000 SF',
     'Under construction: 24,150,000 SF',
     'Under construction one year ago: 31,800,000 SF',
     'Total inventory: 1,023,456,789 SF',
     'Typical free rent: 3 months',
     'Average lease term: 5 years',
     'By size',
     'Under 100,000 SF vacancy 4.9% rent $12.95',
     '100,000-249,999 SF vacancy 6.4% rent $10.60',
     '250,000-499,999 SF vacancy 8.1% rent $8.95',
     '500,000 SF and above vacancy 9.6% rent $7.80'],
]


def val(metric, value, printed, quote, **extra):
    return {'market': 'Dallas-Fort Worth', 'metric': metric, 'segment': '', 'period': 'current', 'value': value,
            'printed': printed, 'rent_period': 'yr' if metric == 'asking_rent' else 'na', 'quote': quote, 'page': 2,
            'note': '', **extra}


def extraction_result():
    values = [
        val('vacancy_rate', 7.2, '7.2%', 'Total vacancy rate ended the quarter at 7.2%'),
        val('vacancy_rate', 7.4, '7.4%', 'Vacancy rate prior quarter: 7.4%', period='prior_quarter'),
        val('vacancy_rate', 8.1, '8.1%', 'Vacancy rate one year ago: 8.1%', period='prior_year'),
        val('asking_rent', 9.85, '$9.85', 'Average asking rent was $9.85 per SF per year NNN.'),
        val('asking_rent', 9.40, '$9.40', 'Asking rent one year ago: $9.40', period='prior_year'),
        val('net_absorption_qtr', 6412300, '6,412,300 SF', 'Net absorption totaled 6,412,300 SF'),
        val('deliveries_qtr', 5210000, '5,210,000 SF', 'Deliveries: 5,210,000 SF'),
        val('under_construction', 24150000, '24,150,000 SF', 'Under construction: 24,150,000 SF'),
        val('under_construction', 31800000, '31,800,000 SF', 'Under construction one year ago: 31,800,000 SF',
            period='prior_year'),
        val('inventory', 1023456789, '1,023,456,789 SF', 'Total inventory: 1,023,456,789 SF'),
        val('free_rent_months', 3, '3 months', 'Typical free rent: 3 months'),
        val('lease_term_months', 60, '5 years', 'Average lease term: 5 years'),
        # a prior-period figure for a metric that is not tracked for trends is dropped
        val('free_rent_months', 3, '3 months', 'Typical free rent: 3 months', period='prior_year'),
    ]
    for label, vac, rent in [('Under 100,000 SF', 4.9, 12.95), ('100,000-249,999 SF', 6.4, 10.60),
                             ('250,000-499,999 SF', 8.1, 8.95), ('500,000 SF and above', 9.6, 7.80)]:
        quote = f'{label} vacancy {vac}% rent ${rent:.2f}'
        values.append(val('vacancy_rate', vac, f'{vac}%', quote, segment=label))
        values.append(val('asking_rent', rent, f'${rent:.2f}', quote, segment=label))
    return {**dfw_extraction(), 'values': values}


def load(app, fake_ai):
    fake_ai['extraction'] = extraction_result()
    doc, _ = ingest.create_document(make_pdf(PAGES), 'dfw.pdf')
    ingest.process_document(doc.id)
    assert MetricValue.query.filter_by(status='needs_review').count() == 0
    return next(m for m in data.build_snapshot()['markets'] if m['market'] == 'Dallas-Fort Worth')


def test_comparison_figures_give_direction_from_a_single_report(app, fake_ai):
    m = load(app, fake_ai)
    assert m['quarter'] == 'Q3 2026'                      # prior-period columns do not change the report quarter
    vac = m['metrics']['vacancy_rate']
    assert vac['value'] == 7.2
    assert vac['previous'] == {'value': 7.4, 'quarter': 'Q2 2026', 'doc_id': vac['doc']['id']}
    assert vac['year_ago']['value'] == 8.1 and vac['year_ago']['quarter'] == 'Q3 2025'
    assert [h['quarter'] for h in m['history']] == ['Q3 2026', 'Q2 2026', 'Q3 2025']
    assert m['history'][1]['restated'] is True
    assert MetricValue.query.filter_by(metric='free_rent_months').count() == 1


def test_trend_indicators_are_calculated_from_report_figures(app, fake_ai):
    trend = load(app, fake_ai)['trend']
    by_key = {s['key']: s for s in trend['signals']}
    assert by_key['vacancy_change_year']['value'] == -90 and by_key['vacancy_change_year']['unit'] == 'bps'
    assert by_key['rent_change_year']['value'] == 4.8                       # (9.85 - 9.40) / 9.40
    assert by_key['pipeline_change']['value'] == -24.1                      # 24.15M vs 31.8M
    assert by_key['pipeline_share']['value'] == 2.4                         # 24.15M / 1,023M
    assert by_key['demand_vs_supply']['value'] == 6412300 - 5210000
    assert all(len(s['inputs']) >= 1 and s['detail'] for s in trend['signals'])
    assert trend['direction'] == 'improving' and trend['has_history'] is True


def test_market_with_one_quarter_and_no_comparisons_has_no_direction(app, fake_ai):
    fake_ai['extraction'] = {**dfw_extraction(), 'values': [
        val('vacancy_rate', 7.2, '7.2%', 'Total vacancy rate ended the quarter at 7.2%')]}
    doc, _ = ingest.create_document(make_pdf(PAGES), 'dfw.pdf')
    ingest.process_document(doc.id)
    trend = data.build_snapshot()['markets'][0]['trend']
    assert trend['direction'] == 'unknown' and trend['has_history'] is False


def test_broker_size_bands_map_onto_the_standard_scale(app, fake_ai):
    m = load(app, fake_ai)
    sizes_ = [s for s in m['segments'] if s['kind'] == 'size']
    assert [s['segment'] for s in sizes_] == ['Under 100,000 SF', '100,000-249,999 SF', '250,000-499,999 SF',
                                              '500,000 SF and above']               # smallest first
    assert [(s['bands'], s['fit']) for s in sizes_] == [
        (['b1', 'b2'], 'spans'), (['b3'], 'exact'), (['b4'], 'exact'), (['b5', 'b6'], 'spans')]
    band = m['by_band']
    assert band['b3']['asking_rent']['value'] == 10.60 and band['b3']['asking_rent']['fit'] == 'exact'
    # The broker's "Under 100,000 SF" row serves both of our smaller bands — unchanged, and labelled.
    assert band['b1']['vacancy_rate']['value'] == band['b2']['vacancy_rate']['value'] == 4.9
    assert band['b1']['vacancy_rate']['broker_band'] == 'Under 100,000 SF' and band['b1']['vacancy_rate']['fit'] == 'spans'
    assert band['b6']['asking_rent']['value'] == 7.80


def test_underwriting_sheet_layers_broker_assumption_and_estimate(app, client, fake_ai):
    load(app, fake_ai)
    sheet = client.get('/api/underwriting/Dallas-Fort Worth').get_json()
    assert [b['key'] for b in sheet['bands']] == ['b1', 'b2', 'b3', 'b4', 'b5', 'b6']
    mid = next(b for b in sheet['bands'] if b['key'] == 'b3')['cells']
    assert mid['rent']['broker']['sheet_value'] == 10.60 and mid['rent']['broker']['scope'] == 'size'
    assert mid['free_rent']['broker']['sheet_value'] == 3 and mid['free_rent']['broker']['scope'] == 'market'
    assert mid['lease_term']['broker']['sheet_value'] == 60
    assert mid['lc']['broker'] is None                      # brokers do not publish this
    assert mid['lc']['estimate'] is not None                # starter estimate exists, kept separate
    assert mid['lc']['assumption'] is None

    r = client.put('/api/underwriting/Dallas-Fort Worth/assumptions', json={'band': 'b3', 'field': 'lc', 'value': '6.5'})
    mid = next(b for b in r.get_json()['bands'] if b['key'] == 'b3')['cells']
    assert mid['lc']['assumption']['value'] == 6.5
    other = next(b for b in r.get_json()['bands'] if b['key'] == 'b4')['cells']
    assert other['lc']['assumption'] is None
    r = client.put('/api/underwriting/Dallas-Fort Worth/assumptions',
                   json={'band': 'b3', 'field': 'ti_renewal', 'value': 4, 'all_bands': True})
    assert all(b['cells']['ti_renewal']['assumption']['value'] == 4 for b in r.get_json()['bands'])
    r = client.put('/api/underwriting/Dallas-Fort Worth/assumptions', json={'band': 'b3', 'field': 'lc', 'value': ''})
    assert next(b for b in r.get_json()['bands'] if b['key'] == 'b3')['cells']['lc']['assumption'] is None


def test_underwriting_index_lists_markets_with_and_without_reports(app, client, fake_ai):
    load(app, fake_ai)
    listing = {m['market']: m for m in client.get('/api/underwriting').get_json()['markets']}
    assert listing['Dallas-Fort Worth']['broker_figures'] > 5 and listing['Dallas-Fort Worth']['has_size_data']
    assert listing['Boston']['broker_figures'] == 0 and listing['Boston']['has_estimates']


def test_lease_term_in_years_must_convert_correctly(app, fake_ai):
    result = extraction_result()
    for v in result['values']:
        if v['metric'] == 'lease_term_months':
            v['value'] = 5          # reader forgot to convert 5 years to months
    fake_ai['extraction'] = result
    doc, _ = ingest.create_document(make_pdf(PAGES), 'dfw.pdf')
    ingest.process_document(doc.id)
    held = MetricValue.query.filter_by(metric='lease_term_months').one()
    assert held.status == 'needs_review'


def test_new_columns_are_added_to_an_existing_database(app):
    from sqlalchemy import inspect, text
    import models
    db.session.execute(text('ALTER TABLE gs_value DROP COLUMN period_offset'))
    db.session.commit()
    assert 'period_offset' not in {c['name'] for c in inspect(db.engine).get_columns('gs_value')}
    models.ensure_columns()
    assert 'period_offset' in {c['name'] for c in inspect(db.engine).get_columns('gs_value')}


def test_levels_alone_never_claim_a_direction(app, fake_ai):
    # Positive absorption, published rent growth and a small pipeline — but no earlier-period figures.
    fake_ai['extraction'] = {**dfw_extraction(), 'values': [
        val('vacancy_rate', 7.2, '7.2%', 'Total vacancy rate ended the quarter at 7.2%'),
        val('net_absorption_qtr', 6412300, '6,412,300 SF', 'Net absorption totaled 6,412,300 SF'),
        val('under_construction', 24150000, '24,150,000 SF', 'Under construction: 24,150,000 SF'),
        val('inventory', 1023456789, '1,023,456,789 SF', 'Total inventory: 1,023,456,789 SF')]}
    doc, _ = ingest.create_document(make_pdf(PAGES), 'dfw.pdf')
    ingest.process_document(doc.id)
    trend = data.build_snapshot()['markets'][0]['trend']
    assert trend['direction'] == 'unknown' and len(trend['signals']) >= 2
