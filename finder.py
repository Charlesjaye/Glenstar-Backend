"""Finds newly published broker reports by searching the web.

Broker *web pages* mostly refuse automated visitors, but the report PDFs
themselves usually sit on public file servers. So instead of scraping pages,
this asks Claude (using Anthropic's hosted web search) for direct links to the
latest report for each market that is missing one, then downloads those files
like any browser would. It does not try to defeat any site's access controls:
if a download is refused, the link is recorded so the PDF can be added by hand.

Whatever is found goes through the same read-and-verify pipeline as an upload,
and the market, broker and quarter are taken from the document itself — so a
wrong or outdated search hit cannot put wrong numbers on the site."""
import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta

import requests

import catalog
import data
import extraction
import ingest
import llm
import quarters
import scanner
from models import Document, FinderRun, Job, db

logger = logging.getLogger(__name__)

MAX_LINKS_PER_MARKET = 6
RETRY_AFTER = timedelta(hours=72)             # do not repeat a search that just found nothing
SCHEDULED_LIMIT = int(os.environ.get('FINDER_MAX_PER_RUN', '8'))

SYSTEM = """You locate commercial real estate research reports for an investment firm's research library. \
The firm files the original report documents, so what it needs from you is the web address of the report file \
itself. Search the brokerages' own websites and document servers. Only report links you actually saw in search \
results; if you cannot find a report for the requested quarter, say so rather than offering an older one."""

_LINE = re.compile(r'^\s*(PDF|PAGE)\s*[:\-]\s*(https?://\S+)', re.I | re.M)
_INDUSTRIAL = re.compile(r'(industrial|logistics|warehouse|distribution|marketbeat|figures)', re.I)


def _aliases(market):
    entry = next((m for m in catalog.MARKETS if m['name'] == market), None)
    return [market] + (entry.get('aliases', []) if entry else [])


def build_prompt(market, quarter):
    names = _aliases(market)
    also = f' (also written {", ".join(names[1:4])})' if len(names) > 1 else ''
    q, year = quarter[1], quarter[3:]
    return (
        f'Find the {quarter} industrial market report for {market}{also}.\n\n'
        f'Brokerages that publish one each quarter include JLL, CBRE ("Figures"), Cushman & Wakefield ("MarketBeat"), '
        f'Colliers, Newmark, Avison Young, Savills, Lee & Associates, Kidder Mathews and Transwestern. '
        f'The quarter may be written Q{q} {year}, {q}Q{year[2:]} or {q}Q {year}.\n\n'
        f'I need the direct link to the report PDF where one exists; the report\'s web page is a second-best '
        f'alternative. Reports from several brokerages are welcome. Leave out reports for other quarters, other '
        f'property types (office, retail, multifamily) and other markets.\n\n'
        f'Finish with one line per report, best first, at most {MAX_LINKS_PER_MARKET} lines, in exactly this form:\n'
        f'PDF: <direct link to the PDF file>\nPAGE: <link to the report page, when you have no direct PDF link>\n'
        f'If you found nothing for {quarter}, finish with the single line: NONE'
    )


def parse_links(text, results, market):
    """Candidate links: the ones Claude listed, then any PDF search hits that mention the market."""
    links, seen = [], set()

    def add(kind, url):
        url = url.rstrip('.,;)>]\'"')
        if url not in seen:
            seen.add(url)
            links.append({'kind': kind, 'url': url})

    for kind, url in _LINE.findall(text or ''):
        add(kind.lower(), url)
    needles = [catalog._norm(a) for a in _aliases(market)]
    for r in results or []:
        raw = f'{r["url"]} {r["title"]}'.replace('-', ' ').replace('_', ' ')
        label = catalog._norm(raw)
        if '.pdf' in r['url'].lower() and _INDUSTRIAL.search(raw) and any(n and n in label for n in needles):
            add('pdf', r['url'])
    return links[:MAX_LINKS_PER_MARKET]


def looks_relevant(pages, market):
    """Cheap check before spending an AI read: does the PDF mention the market and industrial property?"""
    raw = ' '.join(pages[:3])[:20000]
    head = catalog._norm(raw)
    if not head:
        return True                        # scanned PDF — let the reader decide
    return bool(_INDUSTRIAL.search(raw)) and any(n and n in head for n in (catalog._norm(a) for a in _aliases(market)))


def _download(url):
    """→ (pdf_bytes, final_url, note). pdf_bytes is None when the link did not yield a PDF."""
    try:
        resp = scanner.fetch(url)
    except scanner.UnsafeURL as exc:
        return None, url, str(exc)
    except requests.RequestException:
        return None, url, 'could not be reached'
    if resp.content[:5] == b'%PDF-':
        if len(resp.content) > extraction.MAX_PDF_BYTES:
            return None, resp.url, 'PDF is larger than 30 MB'
        return resp.content, resp.url, ''
    if scanner.looks_blocked(resp.status_code, resp.text):
        return None, resp.url, 'site refused automated download'
    if resp.status_code != 200:
        return None, resp.url, f'HTTP {resp.status_code}'
    for href in scanner.pdf_candidates(resp.text, resp.url):       # a report page: follow its PDF links
        try:
            pdf = scanner.fetch(href)
        except (requests.RequestException, scanner.UnsafeURL):
            continue
        if pdf.status_code == 200 and pdf.content[:5] == b'%PDF-' and len(pdf.content) <= extraction.MAX_PDF_BYTES:
            return pdf.content, pdf.url, ''
    return None, resp.url, 'no PDF on the page'


def find_for_market(market, quarter):
    """Search for one market's report and ingest what is found. Returns the FinderRun."""
    run = FinderRun(market=market, quarter=quarter)
    db.session.add(run)
    db.session.commit()

    def finish(status, detail, links=None, new=0):
        run.status, run.detail, run.finished_at = status, detail[:600], datetime.utcnow()
        run.links_json, run.new_documents = json.dumps(links or []), new
        db.session.commit()
        return run

    try:
        text, results = llm.search(build_prompt(market, quarter), system=SYSTEM)
    except llm.LLMError as exc:
        return finish('failed', str(exc))

    links = parse_links(text, results, market)
    if not links:
        return finish('nothing', f'No {quarter} report was found in search results.')

    new_docs, refused = [], 0
    for link in links:
        if Document.query.filter_by(report_url=link['url']).first():
            link['result'] = 'already on file'
            continue
        pdf, final_url, note = _download(link['url'])
        if pdf is None:
            link['result'] = note
            refused += 1 if 'refused' in note else 0
            continue
        if Document.query.filter_by(report_url=final_url).first():
            link['result'] = 'already on file'
            continue
        try:
            pages = extraction.pdf_pages(pdf)
        except extraction.DocumentError as exc:
            link['result'] = str(exc)
            continue
        if not looks_relevant(pages, market):
            link['result'] = 'not an industrial report for this market'
            continue
        name = final_url.rsplit('/', 1)[-1].split('?')[0] or 'report.pdf'
        doc, created = ingest.create_document(pdf, name, origin='search', report_url=final_url, pages=pages)
        if not created:
            link['result'] = 'already on file'
            continue
        ingest.process_document(doc.id)
        db.session.refresh(doc)
        if doc.status in ('ready', 'needs_review'):
            link['result'] = f'added: {doc.source} {doc.quarter}'
            link['document_id'] = doc.id
            new_docs.append(doc)
        else:
            link['result'] = doc.error or 'could not be used'

    if new_docs:
        current = [d for d in new_docs if d.quarter == quarter]
        what = ', '.join(sorted({f'{d.source} {d.quarter}' for d in new_docs}))
        note = '' if current else f' — but none is for {quarter} yet'
        return finish('found', f'Added {what}{note}.', links, len(new_docs))
    if refused:
        return finish('nothing', f'Found {len(links)} link(s) but the files could not be downloaded automatically. '
                                 'Open them from the list and drop the PDFs in.', links)
    return finish('nothing', f'Search found {len(links)} link(s), none of which was a usable {quarter} report.', links)


def markets_to_search(include_recent=False):
    """Tracked markets with no report for the latest quarter, least recently searched first."""
    cov = data.coverage()
    quarter = cov['expected_quarter']
    cutoff = datetime.utcnow() - RETRY_AFTER
    last = {}
    for run in FinderRun.query.filter_by(quarter=quarter).all():
        when = run.finished_at or run.started_at
        if run.market not in last or when > last[run.market]:
            last[run.market] = when
    todo = [m['market'] for m in cov['markets'] if m['status'] == 'missing'
            and (include_recent or m['market'] not in last or last[m['market']] < cutoff)]
    todo.sort(key=lambda name: last.get(name, datetime.min))
    return quarter, todo


def _set_job(status, detail):
    job = db.session.get(Job, 'finder') or Job(name='finder')
    job.status, job.detail, job.updated_at = status, json.dumps(detail), datetime.utcnow()
    db.session.add(job)
    db.session.commit()


def job_status():
    job = db.session.get(Job, 'finder')
    if not job:
        return {'status': 'idle', 'total': 0, 'done': 0, 'found': 0, 'current': None}
    detail = json.loads(job.detail or '{}')
    status = job.status
    if status == 'running' and (datetime.utcnow() - job.updated_at).total_seconds() > 1800:
        status = 'idle'                     # left running by a restart
    return {'status': status, **detail, 'updated_at': job.updated_at.isoformat() + 'Z'}


def start(app, markets=None, limit=None, include_recent=False):
    """Search in the background. Returns (started, number_of_markets)."""
    if not llm.api_key_configured():
        raise llm.LLMError('ANTHROPIC_API_KEY is not configured on the backend.')
    if job_status()['status'] == 'running':
        return False, 0
    quarter, todo = markets_to_search(include_recent=include_recent)
    if markets:
        todo = list(markets)
    if limit:
        todo = todo[:limit]
    if not todo:
        return False, 0
    _set_job('running', {'total': len(todo), 'done': 0, 'found': 0, 'current': todo[0], 'quarter': quarter})

    def work():
        with app.app_context():
            found = 0
            try:
                for i, market in enumerate(todo):
                    _set_job('running', {'total': len(todo), 'done': i, 'found': found, 'current': market,
                                         'quarter': quarter})
                    try:
                        run = find_for_market(market, quarter)
                        found += 1 if run.status == 'found' else 0
                    except Exception:
                        logger.exception('Report search for %s failed', market)
                        db.session.rollback()
                _set_job('idle', {'total': len(todo), 'done': len(todo), 'found': found, 'current': None,
                                  'quarter': quarter})
            finally:
                db.session.remove()
        if found:
            import insights
            insights.maybe_regenerate(app)

    threading.Thread(target=work, name='finder', daemon=True).start()
    return True, len(todo)
