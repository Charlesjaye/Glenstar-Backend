"""Document pipeline: store → read → verify → publish.

The same path is used for dropped PDFs, pasted links and files found by the
scanner, so every number on the site has been through identical checks."""
import hashlib
import json
import logging
import threading
from datetime import datetime, timedelta

from sqlalchemy import update

import catalog
import extraction
import llm
import quarters
import verification
from models import Document, Market, MetricValue, db

logger = logging.getLogger(__name__)

# At most two documents are read at once, to stay inside API rate limits.
_slots = threading.Semaphore(2)


def create_document(data, filename, origin='upload', mime='application/pdf', report_url=None, pages=None):
    """Store a new document (status `queued`). Returns (document, created).
    A byte-identical file that is already stored is returned instead of duplicated."""
    digest = hashlib.sha256(data).hexdigest()
    existing = Document.query.filter_by(sha256=digest).first()
    if existing:
        return existing, False
    if pages is None:
        pages = extraction.pdf_pages(data)       # raises DocumentError for unreadable files
    doc = Document(
        sha256=digest, origin=origin, filename=(filename or 'report.pdf')[:300], mime=mime, size=len(data),
        content=data, pages_json=json.dumps(pages), page_count=len(pages),
        has_text=extraction.has_text_layer(pages), report_url=report_url, status='queued',
        queued_at=datetime.utcnow(),
    )
    db.session.add(doc)
    db.session.commit()
    return doc, True


def process_async(app, doc_id):
    thread = threading.Thread(target=_run, args=(app, doc_id), name=f'ingest-{doc_id}', daemon=True)
    thread.start()
    return thread


def _run(app, doc_id):
    with _slots:
        with app.app_context():
            try:
                process_document(doc_id)
            except Exception:
                logger.exception('Processing document %s crashed', doc_id)
                db.session.rollback()
                doc = db.session.get(Document, doc_id)
                if doc:
                    doc.status, doc.error = 'failed', 'An unexpected error occurred while reading this report.'
                    db.session.commit()
            finally:
                db.session.remove()


def _claim(doc_id):
    """Atomically move a document from queued → processing so two workers never read the same file."""
    result = db.session.execute(
        update(Document).where(Document.id == doc_id, Document.status == 'queued').values(status='processing'))
    db.session.commit()
    return result.rowcount == 1


def _market_lookup():
    """Extra name → canonical lookups for markets created from earlier reports."""
    return {catalog._norm(m.name): m.name for m in Market.query.all()}


def ensure_market(name):
    if not Market.query.filter_by(name=name).first():
        db.session.add(Market(name=name, region=catalog.region_for(name), tracked=True))


def process_document(doc_id):
    """Read one queued document and store its checked values. Returns the document."""
    if not _claim(doc_id):
        return db.session.get(Document, doc_id)
    doc = db.session.get(Document, doc_id)
    data, pages = doc.content, doc.pages()
    doc.error = None
    MetricValue.query.filter_by(document_id=doc.id).delete()

    def finish(status, error=None):
        doc.status, doc.error, doc.processed_at = status, error, datetime.utcnow()
        db.session.commit()
        return doc

    if not llm.api_key_configured():
        return finish('failed', 'The AI reader is not set up: ANTHROPIC_API_KEY is not configured on the backend.')

    # ── Pass 1: read the report ────────────────────────────────────────────
    try:
        result = extraction.extract(data, doc.mime, pages, doc.filename)
    except llm.LLMError as exc:
        return finish('failed', str(exc))
    doc.model = llm.MODEL
    doc.title = (result.get('report_title') or '')[:300]
    if result.get('brokerage') in catalog.BROKERAGES and result['brokerage'] != 'Other':
        doc.source = result['brokerage']
    else:
        doc.source = catalog.normalize_brokerage(result.get('brokerage_as_printed'))
    doc.scope = result.get('scope')

    if not result.get('is_industrial_report'):
        return finish('rejected', result.get('rejection_reason') or 'This is not an industrial market report.')

    # A quarter set by a person on the report page takes precedence over the AI's reading.
    quarter = (doc.quarter if doc.quarter_manual else None) or quarters.parse(result.get('quarter'))
    if not quarter:
        return finish('rejected', 'The report does not state which quarter it covers, so its figures cannot be dated. '
                                  'If you know the quarter, set it on the report page and re-read it.')
    doc.quarter, doc.quarter_idx = quarter, quarters.index(quarter)

    lookup = _market_lookup()
    primary = None
    if doc.scope == 'single_market' and result.get('primary_market'):
        primary, _ = catalog.canonical_market(result['primary_market'], lookup)
    doc.primary_market = primary

    rows = []
    for raw in result.get('values', []):
        metric = catalog.METRIC_BY_KEY.get(raw.get('metric'))
        if not metric or raw.get('value') is None:
            continue
        market = primary or catalog.canonical_market(raw.get('market'), lookup)[0]
        if not market:
            continue
        period = raw.get('rent_period') if raw.get('metric') == 'asking_rent' else None
        rows.append({
            'market': market, 'market_printed': (raw.get('market') or '')[:160],
            'metric': metric['key'], 'unit': metric['unit'],
            'segment': ' '.join((raw.get('segment') or '').split())[:120],
            'value': float(raw['value']), 'printed': (raw.get('printed') or '')[:120],
            'period': period if period in ('yr', 'mo') else None,
            'quote': raw.get('quote') or '', 'page': raw.get('page'), 'note': (raw.get('note') or '')[:300],
        })

    if not rows:
        return finish('rejected', 'No industrial market statistics for the current quarter were found in this report.')

    # ── Automatic checks ───────────────────────────────────────────────────
    for row in rows:
        row['checks'], row['evidence'], row['page'] = verification.check_value(row, pages, doc.has_text)
        if row['metric'] == 'asking_rent' and not row['period']:
            row['checks'].append({'check': 'rent period', 'ok': False,
                                  'detail': 'The report does not make clear whether this rent is per year or per month.'})
    verification.cross_check(rows)

    # ── Pass 2: independent audit ──────────────────────────────────────────
    audit = None
    try:
        audit = extraction.audit(data, doc.mime, pages, quarter, rows)
    except llm.LLMError as exc:
        logger.warning('Audit pass failed for document %s: %s', doc.id, exc)

    for i, row in enumerate(rows):
        verdict = None
        if audit is not None:
            item = audit.get(i)
            verdict = item['verdict'] if item else 'cannot_find'
            if verdict == 'correct':
                row['checks'].append({'check': 'second-reader audit', 'ok': True,
                                      'detail': 'An independent second read of the report agreed with this value.'})
            else:
                detail = (item or {}).get('comment') or 'The second reader could not confirm this value.'
                corrected = (item or {}).get('corrected_value')
                if corrected is not None:
                    detail += f' Suggested value: {corrected:g}.'
                row['checks'].append({'check': 'second-reader audit', 'ok': False, 'detail': detail})
        else:
            row['checks'].append({'check': 'second-reader audit', 'ok': True, 'skipped': True,
                                  'detail': 'The second-reader audit could not run for this report.'})
        status = verification.decide_status([c for c in row['checks'] if not c.get('skipped')],
                                            row['evidence'], verdict)
        ensure_market(row['market'])
        db.session.add(MetricValue(
            document_id=doc.id, market=row['market'], market_printed=row['market_printed'],
            quarter=quarter, quarter_idx=doc.quarter_idx, metric=row['metric'], segment=row['segment'],
            value=row['value'], unit=row['unit'], period=row['period'], printed=row['printed'],
            quote=row['quote'], page=row['page'], note=row['note'], status=status,
            checks_json=json.dumps(row['checks']),
        ))

    db.session.flush()
    return finish(document_status(doc))


def document_status(doc):
    """ready when nothing is waiting for review; needs_review otherwise."""
    pending = MetricValue.query.filter_by(document_id=doc.id, status='needs_review').count()
    return 'needs_review' if pending else 'ready'


def requeue(doc):
    doc.status, doc.error, doc.queued_at = 'queued', None, datetime.utcnow()
    db.session.commit()


def recover_interrupted():
    """On startup, documents left mid-read by a restart are marked failed so they can be retried."""
    cutoff = datetime.utcnow() - timedelta(minutes=20)
    stale = Document.query.filter(Document.status.in_(('processing', 'queued')), Document.queued_at < cutoff).all()
    for doc in stale:
        doc.status = 'failed'
        doc.error = 'Reading this report was interrupted (the server restarted). Click "Read again" to retry.'
    if stale:
        db.session.commit()
