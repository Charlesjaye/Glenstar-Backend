"""The web-search report finder, with the search itself replaced by a stand-in and a
simulated file server standing in for broker sites."""
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import data
import finder
import llm
from conftest import DFW_PAGES, make_pdf
from models import Document, FinderRun

PDF = make_pdf(DFW_PAGES)
OFFICE = make_pdf([['Dallas Office Market Report Q3 2026', 'Office vacancy rose again as tenants downsized.'] * 6])


class Files(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        routes = {'/dfw-industrial-q3-2026.pdf': (200, 'application/pdf', PDF),
                  '/dallas-office-q3-2026.pdf': (200, 'application/pdf', OFFICE),
                  '/blocked.pdf': (403, 'text/html', b'Access Denied'),
                  '/page': (200, 'text/html', b'<a href="/dfw-industrial-q3-2026.pdf">Industrial Q3 2026</a>')}
        status, ctype, body = routes.get(self.path, (404, 'text/plain', b''))
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope='module')
def site():
    server = HTTPServer(('127.0.0.1', 0), Files)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{server.server_port}'
    server.shutdown()


def stub_search(monkeypatch, text, results=()):
    calls = []
    monkeypatch.setattr(llm, 'search', lambda prompt, **kw: (calls.append(prompt) or (text, list(results))))
    return calls


def test_prompt_asks_for_the_right_market_and_quarter():
    prompt = finder.build_prompt('Dallas-Fort Worth', 'Q3 2026')
    assert 'Q3 2026 industrial market report for Dallas-Fort Worth' in prompt and '3Q26' in prompt and 'PDF:' in prompt


def test_links_are_parsed_from_the_answer_and_from_search_hits():
    text = 'Found two.\nPDF: https://files.example.com/dfw-industrial-q3-2026.pdf.\nPAGE: https://www.example.com/dfw\n'
    results = [{'url': 'https://cdn.example.com/Dallas_Industrial_MarketBeat_Q3.pdf', 'title': 'Dallas MarketBeat'},
               {'url': 'https://cdn.example.com/chicago-industrial.pdf', 'title': 'Chicago industrial'},
               {'url': 'https://cdn.example.com/dallas-office.pdf', 'title': 'Dallas office'}]
    links = finder.parse_links(text, results, 'Dallas-Fort Worth')
    assert [l['url'] for l in links] == ['https://files.example.com/dfw-industrial-q3-2026.pdf',
                                         'https://www.example.com/dfw',
                                         'https://cdn.example.com/Dallas_Industrial_MarketBeat_Q3.pdf']
    assert finder.parse_links('NONE', [], 'Dallas-Fort Worth') == []


def test_found_pdf_is_downloaded_read_and_published(app, fake_ai, site, monkeypatch):
    stub_search(monkeypatch, f'PDF: {site}/dfw-industrial-q3-2026.pdf')
    run = finder.find_for_market('Dallas-Fort Worth', 'Q3 2026')
    assert run.status == 'found' and run.new_documents == 1
    doc = Document.query.one()
    assert doc.origin == 'search' and doc.quarter == 'Q3 2026'
    assert data.build_snapshot()['markets'][0]['metrics']['vacancy_rate']['value'] == 7.2
    cov = next(m for m in data.coverage()['markets'] if m['market'] == 'Dallas-Fort Worth')
    assert cov['status'] == 'current' and cov['search']['status'] == 'found'


def test_report_page_link_is_followed_to_its_pdf(app, fake_ai, site, monkeypatch):
    stub_search(monkeypatch, f'PAGE: {site}/page')
    assert finder.find_for_market('Dallas-Fort Worth', 'Q3 2026').status == 'found'


def test_irrelevant_pdf_is_skipped_without_an_ai_read(app, fake_ai, site, monkeypatch):
    stub_search(monkeypatch, f'PDF: {site}/dallas-office-q3-2026.pdf')
    run = finder.find_for_market('Dallas-Fort Worth', 'Q3 2026')
    assert run.status == 'nothing' and fake_ai['calls'] == 0 and Document.query.count() == 0


def test_refused_download_is_reported_with_the_link(app, fake_ai, site, monkeypatch):
    stub_search(monkeypatch, f'PDF: {site}/blocked.pdf')
    run = finder.find_for_market('Dallas-Fort Worth', 'Q3 2026')
    assert run.status == 'nothing' and 'drop the PDFs in' in run.detail
    assert 'refused' in run.links_json and Document.query.count() == 0


def test_nothing_found_and_search_errors_are_recorded(app, fake_ai, monkeypatch):
    stub_search(monkeypatch, 'NONE')
    assert finder.find_for_market('Boston', 'Q3 2026').status == 'nothing'

    def boom(prompt, **kw):
        raise llm.LLMError('Web search is not enabled for this API key.')
    monkeypatch.setattr(llm, 'search', boom)
    run = finder.find_for_market('Boston', 'Q3 2026')
    assert run.status == 'failed' and 'not enabled' in run.detail


def test_only_missing_markets_are_searched_and_not_repeatedly(app, fake_ai, site, monkeypatch):
    quarter, todo = finder.markets_to_search()
    assert len(todo) == 36
    stub_search(monkeypatch, 'NONE')
    finder.find_for_market('Boston', quarter)
    stub_search(monkeypatch, f'PDF: {site}/dfw-industrial-q3-2026.pdf')
    finder.find_for_market('Dallas-Fort Worth', quarter)
    _, todo = finder.markets_to_search()
    assert 'Boston' not in todo and 'Dallas-Fort Worth' not in todo and len(todo) == 34   # searched recently / now current
    _, everything = finder.markets_to_search(include_recent=True)
    assert 'Boston' in everything and 'Dallas-Fort Worth' not in everything
    assert FinderRun.query.count() == 2


def test_inbound_hook_files_attachments_and_links(app, client, fake_ai, site, monkeypatch):
    import io
    import app as app_module
    assert client.post('/api/inbound').status_code == 403                 # disabled without a passcode
    monkeypatch.setattr(app_module, 'ADMIN_TOKEN', 's3cret')
    monkeypatch.setattr(app_module, '_process', lambda doc_id: None)
    assert client.post('/api/inbound?token=wrong').status_code == 401
    r = client.post('/api/inbound?token=s3cret', data={'attachment-1': (io.BytesIO(PDF), 'DFW Q3.pdf'),
                                                       'attachment-2': (io.BytesIO(b'hello'), 'logo.png'),
                                                       'body-plain': f'New report: {site}/dallas-office-q3-2026.pdf'},
                    content_type='multipart/form-data')
    out = r.get_json()
    assert len(out['documents']) == 2 and out['skipped'] == ['logo.png']
    assert {d.origin for d in Document.query.all()} == {'email'}
