"""The scanner against a simulated broker site: blocked pages, dead links,
pages with a PDF, and pages with nothing new."""
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import data
import scanner
from conftest import DFW_PAGES, make_pdf
from models import Document, ScanTarget, db

PDF = make_pdf(DFW_PAGES)


class Site(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        routes = {
            '/blocked': (403, 'text/html', b'<html>Forbidden</html>'),
            '/challenge': (200, 'text/html', b'<html><title>Just a moment...</title>Verify you are human</html>'),
            '/gone': (404, 'text/html', b'not found'),
            '/reports': (200, 'text/html',
                         b'<html><body><h1>Dallas Industrial</h1>'
                         b'<a href="/files/dallas-office-q3-2026.pdf">Office report</a>'
                         b'<a href="/files/dallas-industrial-q3-2026.pdf">Industrial Insight Q3 2026</a>'
                         b'</body></html>'),
            '/files/dallas-industrial-q3-2026.pdf': (200, 'application/pdf', PDF),
            '/files/dallas-office-q3-2026.pdf': (200, 'application/pdf', b'%PDF-1.4 office'),
            '/empty': (200, 'text/html', b'<html><body>' + b'<p>Welcome to our research hub.</p>' * 80 + b'</body></html>'),
        }
        status, ctype, body = routes.get(self.path, (404, 'text/plain', b''))
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope='module')
def site():
    server = HTTPServer(('127.0.0.1', 0), Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{server.server_port}'
    server.shutdown()


def target(path, site):
    ScanTarget.query.filter_by(source='JLL', market='Dallas-Fort Worth').delete()
    t = ScanTarget(source='JLL', market='Dallas-Fort Worth', url=site + path)
    db.session.add(t)
    db.session.commit()
    return t


def test_blocked_site_is_reported_not_guessed(app, fake_ai, site):
    t = target('/blocked', site)
    assert scanner.check_target(t) == 'blocked'
    assert t.http_status == 403 and 'drop it in' in t.detail
    assert Document.query.count() == 0 and data.build_snapshot()['markets'] == []
    assert fake_ai['calls'] == 0


def test_bot_challenge_page_counts_as_blocked(app, fake_ai, site):
    assert scanner.check_target(target('/challenge', site)) == 'blocked'
    assert Document.query.count() == 0


def test_dead_link_is_flagged(app, fake_ai, site):
    t = target('/gone', site)
    assert scanner.check_target(t) == 'not_found' and t.http_status == 404


def test_pdf_on_page_is_found_read_and_published(app, fake_ai, site):
    t = target('/reports', site)
    assert scanner.check_target(t) == 'new_report'
    doc = Document.query.one()                        # the office PDF was skipped
    assert doc.origin == 'scan' and doc.quarter == 'Q3 2026' and doc.report_url.endswith('industrial-q3-2026.pdf')
    assert t.last_document_id == doc.id
    dfw = data.build_snapshot()['markets'][0]
    assert dfw['metrics']['vacancy_rate']['value'] == 7.2
    # A second scan finds nothing new and does not call the AI again
    calls = fake_ai['calls']
    assert scanner.check_target(t) == 'no_change' and fake_ai['calls'] == calls


def test_page_with_no_report_changes_nothing(app, fake_ai, site):
    assert scanner.check_target(target('/empty', site)) == 'no_report_found'
    assert Document.query.count() == 0 and fake_ai['calls'] == 0


def test_unreachable_site_is_an_error(app, fake_ai):
    t = target('', 'http://127.0.0.1:9/none')
    assert scanner.check_target(t) == 'error'


def test_coverage_shows_why_a_market_is_missing(app, fake_ai, site):
    ScanTarget.query.delete()
    scanner.check_target(target('/blocked', site))
    row = next(m for m in data.coverage()['markets'] if m['market'] == 'Dallas-Fort Worth')
    assert row['status'] == 'missing' and row['targets'][0]['result'] == 'blocked'


def test_private_addresses_are_refused(monkeypatch):
    monkeypatch.delenv('GLENSTAR_ALLOW_PRIVATE_URLS')
    for bad in ('http://127.0.0.1/x', 'http://localhost/x', 'ftp://example.com/x', 'file:///etc/passwd', 'notaurl'):
        with pytest.raises(scanner.UnsafeURL):
            scanner.validate_url(bad)
