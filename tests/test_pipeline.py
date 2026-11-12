"""End-to-end: a real PDF goes through store → read → verify → publish.
The Claude calls are replaced by stand-ins; everything else is the real code."""
import io

import data
import ingest
from conftest import DFW_PAGES, dfw_extraction, make_pdf
from models import MetricValue


def upload(client, pdf, name='dfw.pdf'):
    r = client.post('/api/documents', data={'files': (io.BytesIO(pdf), name)}, content_type='multipart/form-data')
    assert r.status_code == 200
    return r.get_json()[0]


def process(app, doc_id):
    ingest.process_document(doc_id)
    return app.test_client().get(f'/api/documents/{doc_id}').get_json()


def create(app, pages=DFW_PAGES, name='dfw.pdf'):
    doc, created = ingest.create_document(make_pdf(pages), name)
    return process(app, doc.id)


def test_clean_report_publishes_every_value(app, fake_ai):
    doc = create(app)
    assert doc['status'] == 'ready'
    assert (doc['source'], doc['quarter'], doc['primary_market']) == ('JLL', 'Q3 2026', 'Dallas-Fort Worth')
    assert {v['status'] for v in doc['values']} == {'verified'}
    snap = data.build_snapshot()
    dfw = next(m for m in snap['markets'] if m['market'] == 'Dallas-Fort Worth')
    assert dfw['quarter'] == 'Q3 2026' and dfw['is_current']
    assert dfw['metrics']['vacancy_rate']['value'] == 7.2
    assert dfw['metrics']['net_absorption_ytd']['value'] == 18_400_000
    v = dfw['metrics']['asking_rent']
    assert (v['value'], v['period'], v['page'], v['doc']['source']) == (9.85, 'yr', 2, 'JLL')
    assert 'asking rent was $9.85' in v['quote']


def test_hallucinated_value_never_reaches_the_site(app, fake_ai):
    # The reader "returns" a cap rate the report does not contain.
    fake_ai['extraction'] = dfw_extraction(extra=[{
        'market': 'Dallas-Fort Worth', 'metric': 'cap_rate', 'segment': '', 'value': 5.4, 'printed': '5.4%',
        'rent_period': 'na', 'quote': 'Cap rates averaged 5.4% for Class A product.', 'page': 2, 'note': ''}])
    doc = create(app)
    assert doc['status'] == 'needs_review'
    cap = next(v for v in doc['values'] if v['metric'] == 'cap_rate')
    assert cap['status'] == 'needs_review'
    dfw = data.build_snapshot()['markets'][0]
    assert 'cap_rate' not in dfw['metrics']           # held back
    assert dfw['metrics']['vacancy_rate']['value'] == 7.2   # the genuine values still publish


def test_misread_number_is_held(app, fake_ai):
    fake_ai['extraction'] = dfw_extraction(overrides={0: {'value': 7.5}})    # printed 7.2%, stored 7.5
    doc = create(app)
    vac = next(v for v in doc['values'] if v['metric'] == 'vacancy_rate')
    assert vac['status'] == 'needs_review'
    assert 'vacancy_rate' not in data.build_snapshot()['markets'][0]['metrics']


def test_audit_disagreement_holds_the_value(app, fake_ai):
    fake_ai['audit'] = lambda values: {
        i: {'index': i, 'verdict': 'incorrect' if v['metric'] == 'asking_rent' else 'correct',
            'corrected_value': None, 'comment': 'This is the direct rent, not the overall average.'}
        for i, v in enumerate(values)}
    doc = create(app)
    rent = next(v for v in doc['values'] if v['metric'] == 'asking_rent')
    assert rent['status'] == 'needs_review'
    assert any('direct rent' in c['detail'] for c in rent['checks'])


def test_review_actions_publish_or_remove(app, client, fake_ai):
    fake_ai['extraction'] = dfw_extraction(overrides={0: {'value': 7.5}})
    doc = create(app)
    vac = next(v for v in doc['values'] if v['metric'] == 'vacancy_rate')
    # Correct it to what the PDF says
    r = client.patch(f'/api/values/{vac["id"]}', json={'action': 'edit', 'value': 7.2})
    assert r.get_json()['status'] == 'edited' and r.get_json()['original_value'] == 7.5
    assert client.get(f'/api/documents/{doc["id"]}').get_json()['status'] == 'ready'
    assert data.build_snapshot()['markets'][0]['metrics']['vacancy_rate']['value'] == 7.2
    # Reject removes it from the site
    client.patch(f'/api/values/{vac["id"]}', json={'action': 'reject'})
    assert 'vacancy_rate' not in data.build_snapshot()['markets'][0]['metrics']


def test_non_industrial_report_is_rejected(app, fake_ai):
    fake_ai['extraction'] = {**dfw_extraction(), 'is_industrial_report': False,
                             'rejection_reason': 'This is an office market report.', 'values': []}
    doc = create(app)
    assert doc['status'] == 'rejected' and 'office' in doc['error']
    assert data.build_snapshot()['markets'] == []


def test_report_without_a_quarter_is_not_dated_by_guesswork(app, client, fake_ai):
    fake_ai['extraction'] = {**dfw_extraction(), 'quarter': None}
    doc = create(app)
    assert doc['status'] == 'rejected' and doc['quarter'] is None
    assert MetricValue.query.count() == 0
    # A person can supply the quarter and have it re-read
    client.patch(f'/api/documents/{doc["id"]}', json={'quarter': 'Q3 2026'})
    ingest.requeue(ingest.db.session.get(ingest.Document, doc['id']))
    assert process(app, doc['id'])['status'] == 'ready'


def test_duplicate_upload_is_not_read_twice(app, client, fake_ai):
    pdf = make_pdf(DFW_PAGES)
    first = upload(client, pdf)
    second = upload(client, pdf, 'copy.pdf')
    assert first['ok'] and second['duplicate'] and first['document']['id'] == second['document']['id']


def test_non_pdf_is_refused(client, fake_ai):
    result = upload(client, b'hello, I am a word document', 'notes.docx')
    assert result['ok'] is False and 'not a PDF' in result['error']


def test_missing_api_key_fails_clearly(app, monkeypatch):
    import llm
    monkeypatch.setattr(llm, 'api_key_configured', lambda: False)
    doc = create(app)
    assert doc['status'] == 'failed' and 'ANTHROPIC_API_KEY' in doc['error']


def test_history_is_kept_and_latest_quarter_wins(app, fake_ai):
    create(app)                                                    # Q3 2026
    q2 = [[l.replace('Q3 2026', 'Q2 2026') for l in DFW_PAGES[0]],
          [l.replace('7.2%', '7.4%') for l in DFW_PAGES[1]]]
    fake_ai['extraction'] = {**dfw_extraction(overrides={0: {
        'value': 7.4, 'printed': '7.4%', 'quote': 'Total vacancy rate ended the quarter at 7.4%'}}),
        'quarter': 'Q2 2026'}
    create(app, q2, 'dfw-q2.pdf')
    dfw = data.build_snapshot()['markets'][0]
    assert dfw['quarter'] == 'Q3 2026' and dfw['metrics']['vacancy_rate']['value'] == 7.2
    assert [h['quarter'] for h in dfw['history']] == ['Q3 2026', 'Q2 2026']
    # Same broker in both quarters → quarter-over-quarter comparison is offered
    assert dfw['metrics']['vacancy_rate']['previous'] == {
        'value': 7.4, 'quarter': 'Q2 2026', 'doc_id': dfw['history'][1]['doc_id']}


def test_stale_market_is_flagged_not_relabelled(app, fake_ai):
    q1 = [[l.replace('Q3 2026', 'Q1 2026') for l in DFW_PAGES[0]], DFW_PAGES[1]]
    fake_ai['extraction'] = {**dfw_extraction(), 'quarter': 'Q1 2026'}
    create(app, q1)
    dfw = data.build_snapshot()['markets'][0]
    assert dfw['quarter'] == 'Q1 2026' and dfw['is_current'] is False and dfw['quarters_behind'] >= 1
    cov = next(m for m in data.coverage()['markets'] if m['market'] == 'Dallas-Fort Worth')
    assert cov['status'] == 'missing' and cov['latest_quarter'] == 'Q1 2026'


def test_coverage_tracks_missing_review_and_current(app, fake_ai):
    cov = data.coverage()
    assert cov['counts']['missing'] == cov['counts']['total'] == 36
    fake_ai['audit'] = 'incorrect'                     # everything held for review
    doc = create(app)
    row = next(m for m in data.coverage()['markets'] if m['market'] == 'Dallas-Fort Worth')
    assert row['status'] == 'review' and row['review_documents'] == [doc['id']]
    app.test_client().post(f'/api/documents/{doc["id"]}/confirm-all')
    row = next(m for m in data.coverage()['markets'] if m['market'] == 'Dallas-Fort Worth')
    assert row['status'] == 'current' and row['sources'] == ['JLL']


def test_multi_market_report_fills_several_markets(app, fake_ai):
    pages = [['Cushman & Wakefield', 'U.S. Industrial MarketBeat Q3 2026',
              'National industrial fundamentals continued to stabilise in the third quarter as new supply slowed',
              'and occupiers returned to the market. Statistics by market are shown on the following page.'],
             ['Market Vacancy Asking Rent', 'Atlanta 8.1% $7.95', 'Dallas/Fort Worth 9.0% $8.40', 'Reno 10.2% $9.10']]

    def row(market, vac, rent):
        return [
            {'market': market, 'metric': 'vacancy_rate', 'segment': '', 'value': vac, 'printed': f'{vac}%',
             'rent_period': 'na', 'quote': f'{market} {vac}%', 'page': 2, 'note': ''},
            {'market': market, 'metric': 'asking_rent', 'segment': '', 'value': rent, 'printed': f'${rent:.2f}',
             'rent_period': 'yr', 'quote': f'{market} {vac}% ${rent:.2f}', 'page': 2, 'note': ''}]
    fake_ai['extraction'] = {**dfw_extraction(), 'brokerage': 'Cushman & Wakefield', 'scope': 'multi_market',
                             'primary_market': '', 'report_title': 'U.S. Industrial MarketBeat',
                             'values': row('Atlanta', 8.1, 7.95) + row('Dallas/Fort Worth', 9.0, 8.40) + row('Reno', 10.2, 9.10)}
    create(app, pages, 'us.pdf')
    names = [m['market'] for m in data.build_snapshot()['markets']]
    assert names == ['Atlanta', 'Dallas-Fort Worth', 'Reno']     # alias mapped; unknown market kept as printed


def test_two_brokers_are_never_blended(app, fake_ai):
    create(app)
    pages = [['CBRE', 'Dallas-Fort Worth Industrial Figures Q3 2026',
              'Leasing demand remained healthy across the metroplex in the third quarter, led by logistics users',
              'in the South Dallas and North Fort Worth submarkets, while construction starts continued to slow.'],
             ['Vacancy rate 7.9%', 'Availability rate 10.4%']]
    fake_ai['extraction'] = {**dfw_extraction(), 'brokerage': 'CBRE', 'values': [
        {'market': 'Dallas-Fort Worth', 'metric': 'vacancy_rate', 'segment': '', 'value': 7.9, 'printed': '7.9%',
         'rent_period': 'na', 'quote': 'Vacancy rate 7.9%', 'page': 2, 'note': ''},
        {'market': 'Dallas-Fort Worth', 'metric': 'availability_rate', 'segment': '', 'value': 10.4,
         'printed': '10.4%', 'rent_period': 'na', 'quote': 'Availability rate 10.4%', 'page': 2, 'note': ''}]}
    create(app, pages, 'cbre.pdf')
    dfw = data.build_snapshot()['markets'][0]
    assert dfw['primary_source'] == 'JLL' and dfw['sources'] == ['CBRE', 'JLL']
    assert dfw['metrics']['vacancy_rate']['value'] == 7.2                  # JLL's own figure, not an average
    assert dfw['metrics']['availability_rate']['doc']['source'] == 'CBRE'  # gap filled, labelled with its source


def test_document_file_is_served_back(app, client, fake_ai):
    doc = create(app)
    r = client.get(f'/api/documents/{doc["id"]}/file')
    assert r.status_code == 200 and r.data[:5] == b'%PDF-' and r.mimetype == 'application/pdf'


def test_admin_passcode_protects_changes(app, client, fake_ai, monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module, 'ADMIN_TOKEN', 's3cret')
    pdf = make_pdf(DFW_PAGES)
    r = client.post('/api/documents', data={'files': (io.BytesIO(pdf), 'a.pdf')}, content_type='multipart/form-data')
    assert r.status_code == 401
    r = client.post('/api/documents', data={'files': (io.BytesIO(pdf), 'a.pdf')},
                    content_type='multipart/form-data', headers={'X-Admin-Token': 's3cret'})
    assert r.status_code == 200
    assert client.get('/api/markets').status_code == 200          # reading stays open
