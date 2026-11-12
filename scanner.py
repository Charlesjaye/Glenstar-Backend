"""Checks broker web pages for newly published reports.

The scanner never produces numbers itself. It only tries to *find a document*
(a PDF linked from the page, or the page's own text) and hands it to the same
read-and-verify pipeline as an uploaded PDF. When a site blocks automated
access — most broker sites do — the target is recorded as `blocked` so the
site can ask for the PDF to be dropped in instead. Nothing is ever guessed or
carried forward from an older quarter."""
import ipaddress
import logging
import os
import re
import socket
import threading
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import catalog
import extraction
import ingest
import llm
import quarters
from models import PUBLISHED, Document, MetricValue, ScanRun, ScanTarget, db

logger = logging.getLogger(__name__)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
                  'Chrome/130.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}
TIMEOUT = 25
MAX_PDFS_PER_PAGE = 3

_BLOCK_MARKERS = re.compile(
    r'(just a moment\.\.\.|cf-chl|cf_chl|attention required|access denied|request blocked|are you a robot|'
    r'verify you are (a )?human|captcha|px-captcha|perimeterx|incapsula|imperva|akamai.{0,40}reference #|'
    r'bot detection|unusual traffic|enable javascript and cookies)', re.I)
_INDUSTRIAL = re.compile(r'(industrial|logistics|warehouse|marketbeat|figures|distribution)', re.I)
_OTHER_SECTOR = re.compile(r'(office|retail|multifamily|multi-family|apartment|hotel|hospitality|life.?science|medical)', re.I)

RESULT_TEXT = {
    'new_report': 'New report found and read',
    'no_change': 'Checked — nothing new',
    'blocked': 'Site blocked automated access',
    'not_found': 'Page not found — the link needs updating',
    'no_report_found': 'Page loaded but no report could be found on it',
    'error': 'Could not reach the site',
    'needs_api_key': 'Found a report but the AI reader is not set up',
}


def sync_targets():
    """Make sure every catalog page has a scan target (existing rows, including edited URLs, are kept)."""
    existing = {(t.source, t.market) for t in ScanTarget.query.all()}
    for page in catalog.SOURCE_PAGES:
        if (page['source'], page['market']) not in existing:
            db.session.add(ScanTarget(source=page['source'], market=page['market'], url=page['url']))
    db.session.commit()


class UnsafeURL(ValueError):
    pass


def validate_url(url):
    """Only public http(s) addresses may be fetched — never the server's own network."""
    parsed = urlparse((url or '').strip())
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise UnsafeURL('Enter a full web address starting with https://')
    if os.environ.get('GLENSTAR_ALLOW_PRIVATE_URLS') == '1':
        return parsed.geturl()
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80))
    except socket.gaierror:
        raise UnsafeURL('That web address could not be found.')
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise UnsafeURL('That address is not a public website.')
    return parsed.geturl()


def fetch(url):
    validate_url(url)
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
    if resp.history:
        validate_url(resp.url)
    return resp


def looks_blocked(status, body_text):
    if status in (401, 403, 406, 429, 503):
        return True
    return len(body_text) < 6000 and bool(_BLOCK_MARKERS.search(body_text))


def pdf_candidates(html, base_url):
    """PDF links on a page that look like industrial market reports, best first."""
    soup = BeautifulSoup(html, 'html.parser')
    scored, seen = [], set()
    for a in soup.find_all('a', href=True):
        href = urljoin(base_url, a['href'].strip())
        if '.pdf' not in href.lower() or href in seen or urlparse(href).scheme not in ('http', 'https'):
            continue
        seen.add(href)
        label = f'{href} {a.get_text(" ", strip=True)}'
        if _OTHER_SECTOR.search(label) and not _INDUSTRIAL.search(label):
            continue
        score = 0
        if _INDUSTRIAL.search(label):
            score += 3
        q = quarters.parse(label.replace('-', ' ').replace('_', ' '))
        if q:
            score += 2 + max(0, quarters.index(q) - quarters.index(quarters.latest_published()) + 4) / 10
        scored.append((score, href))
    scored.sort(key=lambda x: -x[0])
    return [href for score, href in scored if score > 0][:MAX_PDFS_PER_PAGE]


def page_text(html):
    soup = BeautifulSoup(html, 'html.parser')
    for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'noscript', 'svg', 'form']):
        tag.decompose()
    return re.sub(r'[ \t]+', ' ', soup.get_text(separator='\n', strip=True))


def _have_data_for(source, market, quarter):
    return db.session.query(MetricValue.id).join(Document).filter(
        Document.source == source, MetricValue.market == market,
        MetricValue.quarter == quarter, MetricValue.status.in_(PUBLISHED)).first() is not None


def _ingest(data, filename, mime, url, pages=None):
    """Store and read a found document. Returns (document or None, was_new)."""
    doc, created = ingest.create_document(data, filename, origin='scan', mime=mime, report_url=url, pages=pages)
    if created:
        ingest.process_document(doc.id)
        db.session.refresh(doc)
    return doc, created


def check_target(target):
    """Check one broker page. Updates and returns the target's result."""
    def done(result, detail='', http=None, doc=None):
        target.last_checked, target.last_result = datetime.utcnow(), result
        target.detail, target.http_status = (detail or RESULT_TEXT[result])[:400], http
        if doc is not None:
            target.last_document_id = doc.id
        db.session.commit()
        return result

    try:
        resp = fetch(target.url)
    except UnsafeURL as exc:
        return done('error', str(exc))
    except requests.RequestException as exc:
        return done('error', f'Could not reach the site ({exc.__class__.__name__}).')

    ctype = (resp.headers.get('Content-Type') or '').lower()
    is_pdf = 'application/pdf' in ctype or resp.content[:5] == b'%PDF-'
    body = '' if is_pdf else resp.text

    if resp.status_code in (404, 410):
        return done('not_found', http=resp.status_code)
    if looks_blocked(resp.status_code, body):
        return done('blocked', f'The site refused automated access (HTTP {resp.status_code}). '
                               'Download the PDF from the page and drop it in.', resp.status_code)
    if resp.status_code >= 400:
        return done('error', f'The site returned an error (HTTP {resp.status_code}).', resp.status_code)

    found = []
    if is_pdf:
        found.append((target.url, resp.content))
    else:
        for href in pdf_candidates(body, resp.url):
            if Document.query.filter_by(report_url=href).first():
                continue
            try:
                pdf = fetch(href)
            except (requests.RequestException, UnsafeURL):
                continue
            if pdf.status_code == 200 and pdf.content[:5] == b'%PDF-' and len(pdf.content) <= extraction.MAX_PDF_BYTES:
                found.append((href, pdf.content))

    if found and not llm.api_key_configured():
        return done('needs_api_key', http=resp.status_code)

    new_doc = None
    for href, data in found:
        try:
            doc, created = _ingest(data, href.rsplit('/', 1)[-1].split('?')[0] or 'report.pdf', 'application/pdf', href)
        except extraction.DocumentError:
            continue
        if created and doc.status in ('ready', 'needs_review'):
            new_doc = new_doc or doc
    if new_doc:
        return done('new_report', f'Found and read: {new_doc.title or new_doc.filename} ({new_doc.quarter}).',
                    resp.status_code, new_doc)
    if found or is_pdf:
        return done('no_change', http=resp.status_code)

    # No PDF link: fall back to statistics published in the page itself — only when the page
    # names a quarter we do not already hold for this broker and market.
    text = page_text(body)
    page_quarter = quarters.parse(text[:6000])
    if (len(text) >= 1500 and page_quarter and _INDUSTRIAL.search(text) and re.search(r'\d(\.\d+)?\s*%', text)
            and not _have_data_for(target.source, target.market, page_quarter)):
        if not llm.api_key_configured():
            return done('needs_api_key', http=resp.status_code)
        name = f'{target.source} {target.market} web page ({page_quarter}).txt'
        doc, created = _ingest(text.encode('utf-8'), name, 'text/plain', target.url, pages=[text])
        if created and doc.status in ('ready', 'needs_review'):
            return done('new_report', f'Read statistics published on the page ({doc.quarter}).', resp.status_code, doc)
        return done('no_report_found', 'The page loaded but no usable current-quarter statistics were found on it.',
                    resp.status_code)
    if page_quarter and _have_data_for(target.source, target.market, page_quarter):
        return done('no_change', http=resp.status_code)
    return done('no_report_found', http=resp.status_code)


def start_run(app, trigger='manual'):
    """Start a scan in the background unless one is already running. Returns (run, started)."""
    recent = datetime.utcnow() - timedelta(minutes=45)
    running = ScanRun.query.filter(ScanRun.status == 'running', ScanRun.started_at > recent).first()
    if running:
        return running, False
    run = ScanRun(trigger=trigger, total=ScanTarget.query.count())
    db.session.add(run)
    db.session.commit()
    threading.Thread(target=_run, args=(app, run.id), name=f'scan-{run.id}', daemon=True).start()
    return run, True


def _run(app, run_id):
    with app.app_context():
        run = db.session.get(ScanRun, run_id)
        try:
            for target_id in [t.id for t in ScanTarget.query.order_by(ScanTarget.source, ScanTarget.market).all()]:
                target = db.session.get(ScanTarget, target_id)
                try:
                    result = check_target(target)
                except Exception:
                    logger.exception('Scan of %s failed', target.url)
                    db.session.rollback()
                    result = 'error'
                run = db.session.get(ScanRun, run_id)
                run.checked += 1
                run.new_documents += 1 if result == 'new_report' else 0
                run.blocked += 1 if result == 'blocked' else 0
                db.session.commit()
            run.status = 'done'
        except Exception:
            logger.exception('Scan run %s failed', run_id)
            db.session.rollback()
            run = db.session.get(ScanRun, run_id)
            run.status = 'failed'
        run.finished_at = datetime.utcnow()
        db.session.commit()
        new_documents = run.new_documents
        db.session.remove()
    if new_documents:
        import insights
        insights.regenerate_async(app, reason='new reports from scan')
