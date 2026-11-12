"""Glenstar Intelligence API.

Every market statistic served by this API comes from a stored broker document
and carries the page and quote it was read from. There is no seed data."""
import hmac
import io
import logging
import os
import threading
from datetime import datetime
from functools import wraps

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
from sqlalchemy.exc import IntegrityError

import catalog
import data
import extraction
import ingest
import insights
import llm
import quarters
import scanner
from models import PUBLISHED, Document, Market, MetricValue, ScanRun, ScanTarget, Thesis, db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _database_url():
    url = os.environ.get('DATABASE_URL', 'sqlite:///glenstar.db')
    # Hosting providers hand out postgres:// URLs; SQLAlchemy needs postgresql://
    return 'postgresql://' + url[len('postgres://'):] if url.startswith('postgres://') else url


app = Flask(__name__)
CORS(app, expose_headers=['Content-Disposition'])
app.config['SQLALCHEMY_DATABASE_URI'] = _database_url()
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'pool_pre_ping': True}
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024
db.init_app(app)

ADMIN_TOKEN = os.environ.get('ADMIN_TOKEN', '')


def iso(dt):
    return dt.isoformat() + 'Z' if dt else None


def error(message, status=400):
    return jsonify({'error': message}), status


def admin(fn):
    """Protects endpoints that change data. Enforced only when ADMIN_TOKEN is set."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if ADMIN_TOKEN:
            supplied = request.headers.get('X-Admin-Token', '')
            if not hmac.compare_digest(supplied.encode(), ADMIN_TOKEN.encode()):
                return error('Enter the admin passcode to make changes.', 401)
        return fn(*args, **kwargs)
    return wrapper


@app.errorhandler(413)
def too_large(_):
    return error('That upload is too large. Upload fewer files at once (each PDF can be up to 30 MB).', 413)


# ── Status ───────────────────────────────────────────────────────────────────

@app.route('/api/health')
def health():
    return jsonify({'status': 'ok', 'time': iso(datetime.utcnow())})


@app.route('/api/stats')
def stats():
    cov = data.coverage()
    snapshot = data.build_snapshot()
    thesis = Thesis.query.filter_by(is_current=True).first()
    last_scan = ScanRun.query.order_by(ScanRun.started_at.desc()).first()
    review_docs = (db.session.query(MetricValue.document_id)
                   .filter(MetricValue.status == 'needs_review').distinct().count())
    uri = app.config['SQLALCHEMY_DATABASE_URI']
    _, _, fingerprint = insights.thesis_inputs(snapshot)
    return jsonify({
        'expected_quarter': cov['expected_quarter'],
        'calendar_quarter': quarters.calendar_quarter(),
        'api_key_configured': llm.api_key_configured(),
        'admin_required': bool(ADMIN_TOKEN),
        'storage_persistent': not uri.startswith('sqlite'),
        'market_count': len(snapshot['markets']),
        'current_market_count': sum(1 for m in snapshot['markets'] if m['is_current']),
        'tracked_market_count': cov['counts']['total'],
        'coverage': cov['counts'],
        'document_count': Document.query.filter(Document.status.in_(('ready', 'needs_review'))).count(),
        'processing_count': Document.query.filter(Document.status.in_(('queued', 'processing'))).count(),
        'review_document_count': review_docs,
        'review_value_count': MetricValue.query.filter_by(status='needs_review').count(),
        'thesis_quarter': thesis.quarter if thesis else None,
        'thesis_generated_at': iso(thesis.generated_at) if thesis else None,
        'thesis_stale': bool(thesis and fingerprint and thesis.data_fingerprint != fingerprint),
        'last_scan': {'started_at': iso(last_scan.started_at), 'status': last_scan.status} if last_scan else None,
    })


@app.route('/api/auth/check', methods=['POST'])
@admin
def auth_check():
    return jsonify({'ok': True})


# ── Market data ──────────────────────────────────────────────────────────────

@app.route('/api/markets')
def markets():
    return jsonify(data.build_snapshot())


@app.route('/api/coverage')
def coverage():
    return jsonify(data.coverage())


# ── Documents ────────────────────────────────────────────────────────────────

# Refreshing the thesis costs an AI call, so wait for a quiet period after uploads:
# dropping in twenty PDFs should produce one new thesis, not twenty.
THESIS_QUIET_SECONDS = int(os.environ.get('THESIS_QUIET_SECONDS', '45'))
_thesis_timer = None
_thesis_lock = threading.Lock()


def _schedule_thesis_refresh():
    global _thesis_timer
    with _thesis_lock:
        if _thesis_timer is not None:
            _thesis_timer.cancel()
        _thesis_timer = threading.Timer(THESIS_QUIET_SECONDS, _refresh_thesis)
        _thesis_timer.daemon = True
        _thesis_timer.start()


def _refresh_thesis():
    try:
        insights.maybe_regenerate(app)
    except Exception:
        logger.exception('Thesis refresh failed')


def _process(doc_id):
    def work():
        ingest._run(app, doc_id)
        _schedule_thesis_refresh()
    threading.Thread(target=work, name=f'ingest-{doc_id}', daemon=True).start()


@app.route('/api/documents', methods=['GET'])
def list_documents():
    q = Document.query
    if request.args.get('quarter'):
        q = q.filter_by(quarter=request.args['quarter'])
    if request.args.get('status'):
        q = q.filter(Document.status.in_(request.args['status'].split(',')))
    docs = q.order_by(Document.created_at.desc()).limit(1000).all()
    counts = data.document_counts([d.id for d in docs]) if docs else {}
    return jsonify([data.document_dict(d, counts.get(d.id)) for d in docs])


@app.route('/api/documents', methods=['POST'])
@admin
def upload_documents():
    files = request.files.getlist('files') or request.files.getlist('file')
    if not files:
        return error('No files were received.')
    results = []
    for f in files:
        name = f.filename or 'report.pdf'
        blob = f.read()
        if len(blob) > extraction.MAX_PDF_BYTES:
            results.append({'filename': name, 'ok': False, 'error': 'This file is larger than 30 MB.'})
            continue
        try:
            doc, created = ingest.create_document(blob, name, origin='upload')
        except extraction.DocumentError as exc:
            results.append({'filename': name, 'ok': False, 'error': str(exc)})
            continue
        if created:
            _process(doc.id)
        results.append({'filename': name, 'ok': True, 'duplicate': not created,
                        'document': data.document_dict(doc, data.document_counts([doc.id]).get(doc.id))})
    return jsonify(results)


@app.route('/api/documents/from-url', methods=['POST'])
@admin
def document_from_url():
    url = (request.json or {}).get('url', '').strip()
    try:
        resp = scanner.fetch(url)
    except scanner.UnsafeURL as exc:
        return error(str(exc))
    except Exception:
        return error('That link could not be reached.')
    if scanner.looks_blocked(resp.status_code, '' if resp.content[:5] == b'%PDF-' else resp.text):
        return error('The site refused automated access to that link. Download the PDF in your browser and drop it '
                     'in instead.', 422)
    if resp.status_code != 200:
        return error(f'That link returned an error (HTTP {resp.status_code}).', 422)
    if resp.content[:5] != b'%PDF-':
        return error('That link is a web page, not a PDF. Open it, download the report PDF, and drop it in — '
                     'or paste the direct link to the PDF file.', 422)
    if len(resp.content) > extraction.MAX_PDF_BYTES:
        return error('That PDF is larger than 30 MB.', 422)
    name = resp.url.rsplit('/', 1)[-1].split('?')[0] or 'report.pdf'
    try:
        doc, created = ingest.create_document(resp.content, name, origin='link', report_url=resp.url)
    except extraction.DocumentError as exc:
        return error(str(exc), 422)
    if created:
        _process(doc.id)
    return jsonify({'ok': True, 'duplicate': not created,
                    'document': data.document_dict(doc, data.document_counts([doc.id]).get(doc.id))})


def _get_doc(doc_id):
    return db.session.get(Document, doc_id)


def _value_detail(v, doc):
    d = data.value_dict(v, doc)
    d.update({'checks': v.checks(), 'market_printed': v.market_printed, 'original_value': v.original_value,
              'label': catalog.METRIC_BY_KEY.get(v.metric, {}).get('label', v.metric)})
    return d


@app.route('/api/documents/<int:doc_id>')
def get_document(doc_id):
    doc = _get_doc(doc_id)
    if not doc:
        return error('Report not found.', 404)
    values = MetricValue.query.filter_by(document_id=doc.id).order_by(
        MetricValue.market, MetricValue.segment, MetricValue.id).all()
    out = data.document_dict(doc, data.document_counts([doc.id]).get(doc.id))
    out['values'] = [_value_detail(v, doc) for v in values]
    return jsonify(out)


@app.route('/api/documents/<int:doc_id>/file')
def document_file(doc_id):
    doc = _get_doc(doc_id)
    if not doc:
        return error('Report not found.', 404)
    return send_file(io.BytesIO(doc.content), mimetype=doc.mime or 'application/pdf',
                     as_attachment=False, download_name=doc.filename or f'report-{doc.id}.pdf')


@app.route('/api/documents/<int:doc_id>', methods=['PATCH'])
@admin
def update_document(doc_id):
    doc = _get_doc(doc_id)
    if not doc:
        return error('Report not found.', 404)
    body = request.json or {}
    if 'quarter' in body:
        quarter = quarters.parse(body['quarter'])
        if not quarter:
            return error('Enter the quarter like "Q3 2026".')
        doc.quarter, doc.quarter_idx, doc.quarter_manual = quarter, quarters.index(quarter), True
        MetricValue.query.filter_by(document_id=doc.id).update(
            {'quarter': quarter, 'quarter_idx': doc.quarter_idx})
    if 'source' in body and body['source'].strip():
        doc.source = catalog.normalize_brokerage(body['source'])
    if 'market' in body and body['market'].strip():
        market, _ = catalog.canonical_market(body['market'], ingest._market_lookup())
        ingest.ensure_market(market)
        doc.primary_market, doc.scope = market, 'single_market'
        MetricValue.query.filter_by(document_id=doc.id).update({'market': market})
    db.session.commit()
    return get_document(doc_id)


@app.route('/api/documents/<int:doc_id>/reprocess', methods=['POST'])
@admin
def reprocess_document(doc_id):
    doc = _get_doc(doc_id)
    if not doc:
        return error('Report not found.', 404)
    if doc.status in ('queued', 'processing'):
        return error('This report is already being read.', 409)
    ingest.requeue(doc)
    _process(doc.id)
    return get_document(doc_id)


@app.route('/api/documents/<int:doc_id>', methods=['DELETE'])
@admin
def delete_document(doc_id):
    doc = _get_doc(doc_id)
    if not doc:
        return error('Report not found.', 404)
    db.session.delete(doc)
    db.session.commit()
    return jsonify({'ok': True})


def _refresh_doc_status(doc):
    if doc.status in ('ready', 'needs_review'):
        doc.status = ingest.document_status(doc)


@app.route('/api/documents/<int:doc_id>/confirm-all', methods=['POST'])
@admin
def confirm_all(doc_id):
    doc = _get_doc(doc_id)
    if not doc:
        return error('Report not found.', 404)
    MetricValue.query.filter_by(document_id=doc.id, status='needs_review').update(
        {'status': 'confirmed', 'reviewed_at': datetime.utcnow()})
    db.session.flush()
    _refresh_doc_status(doc)
    db.session.commit()
    return get_document(doc_id)


@app.route('/api/documents/<int:doc_id>/values', methods=['POST'])
@admin
def add_value(doc_id):
    """A person adds a figure the AI missed, citing the page it is on."""
    doc = _get_doc(doc_id)
    if not doc:
        return error('Report not found.', 404)
    if not doc.quarter:
        return error('Set the report quarter first.')
    body = request.json or {}
    metric = catalog.METRIC_BY_KEY.get(body.get('metric'))
    try:
        value = float(body.get('value'))
    except (TypeError, ValueError):
        return error('Enter a number for the value.')
    if not metric:
        return error('Choose a metric.')
    market = doc.primary_market or catalog.canonical_market(body.get('market'), ingest._market_lookup())[0]
    if not market:
        return error('Enter the market this figure is for.')
    ingest.ensure_market(market)
    period = body.get('period') if metric['key'] == 'asking_rent' else None
    if metric['key'] == 'asking_rent' and period not in ('yr', 'mo'):
        return error('Choose whether the rent is per year or per month.')
    v = MetricValue(document_id=doc.id, market=market, market_printed=body.get('market') or market,
                    quarter=doc.quarter, quarter_idx=doc.quarter_idx, metric=metric['key'],
                    segment=' '.join((body.get('segment') or '').split())[:120], value=value, unit=metric['unit'],
                    period=period, printed=str(body.get('printed') or body.get('value'))[:120],
                    quote=(body.get('quote') or '')[:2000], page=body.get('page') or None,
                    note=(body.get('note') or '')[:300], status='manual', reviewed_at=datetime.utcnow(),
                    checks_json='[{"check": "entered by a person", "ok": true, "detail": "Added manually from the report."}]')
    db.session.add(v)
    db.session.commit()
    return get_document(doc_id)


@app.route('/api/values/<int:value_id>', methods=['PATCH'])
@admin
def update_value(value_id):
    v = db.session.get(MetricValue, value_id)
    if not v:
        return error('Value not found.', 404)
    body = request.json or {}
    action = body.get('action')
    if action == 'confirm':
        v.status = 'confirmed'
    elif action == 'reject':
        v.status = 'rejected'
    elif action == 'edit':
        try:
            new_value = float(body.get('value'))
        except (TypeError, ValueError):
            return error('Enter a number.')
        if v.original_value is None:
            v.original_value = v.value
        v.value, v.status = new_value, 'edited'
        if v.metric == 'asking_rent' and body.get('period') in ('yr', 'mo'):
            v.period = body['period']
        if body.get('page'):
            v.page = int(body['page'])
    else:
        return error('Unknown action.')
    v.reviewed_at = datetime.utcnow()
    db.session.flush()
    _refresh_doc_status(v.document)
    db.session.commit()
    return jsonify(_value_detail(v, v.document))


# ── Scanner ──────────────────────────────────────────────────────────────────

def _run_dict(run):
    if not run:
        return None
    return {'id': run.id, 'trigger': run.trigger, 'status': run.status, 'started_at': iso(run.started_at),
            'finished_at': iso(run.finished_at), 'total': run.total, 'checked': run.checked,
            'new_documents': run.new_documents, 'blocked': run.blocked}


@app.route('/api/scan', methods=['POST'])
@admin
def start_scan():
    run, started = scanner.start_run(app, trigger='manual')
    return jsonify({'started': started, 'run': _run_dict(run)})


@app.route('/api/scan/status')
def scan_status():
    run = ScanRun.query.order_by(ScanRun.started_at.desc()).first()
    return jsonify({'run': _run_dict(run), 'schedule': '6:00 AM and 6:00 PM UTC'})


@app.route('/api/scan/targets')
def scan_targets():
    return jsonify([{
        'id': t.id, 'source': t.source, 'market': t.market, 'url': t.url, 'result': t.last_result,
        'detail': t.detail, 'http_status': t.http_status, 'last_checked': iso(t.last_checked),
        'last_document_id': t.last_document_id,
    } for t in ScanTarget.query.order_by(ScanTarget.source, ScanTarget.market).all()])


@app.route('/api/scan/targets/<int:target_id>', methods=['PATCH'])
@admin
def update_target(target_id):
    t = db.session.get(ScanTarget, target_id)
    if not t:
        return error('Source not found.', 404)
    try:
        t.url = scanner.validate_url((request.json or {}).get('url'))
    except scanner.UnsafeURL as exc:
        return error(str(exc))
    t.last_result, t.detail, t.http_status, t.last_checked = None, None, None, None
    db.session.commit()
    return jsonify({'ok': True})


# ── Thesis & chat ────────────────────────────────────────────────────────────

def _thesis_dict(t):
    import json
    return {'id': t.id, 'quarter': t.quarter, 'generated_at': iso(t.generated_at), 'summary': t.summary,
            'rankings': json.loads(t.rankings_json or '[]'), 'risks': json.loads(t.risks_json or '[]'),
            'warnings': json.loads(t.warnings_json or '[]'), 'market_count': t.market_count,
            'document_count': t.document_count, 'is_current': t.is_current}


@app.route('/api/thesis/current')
def thesis_current():
    t = Thesis.query.filter_by(is_current=True).order_by(Thesis.generated_at.desc()).first()
    quarter, markets_in, fingerprint = insights.thesis_inputs()
    return jsonify({
        'thesis': _thesis_dict(t) if t else None,
        'job': insights.job_status(),
        'stale': bool(t and fingerprint and t.data_fingerprint != fingerprint),
        'can_generate': bool(markets_in) and llm.api_key_configured(),
        'available_quarter': quarter,
        'available_market_count': len(markets_in),
        'min_markets': insights.MIN_MARKETS,
        'api_key_configured': llm.api_key_configured(),
    })


@app.route('/api/thesis/history')
def thesis_history():
    rows = Thesis.query.order_by(Thesis.generated_at.desc()).limit(24).all()
    return jsonify([{'id': t.id, 'quarter': t.quarter, 'generated_at': iso(t.generated_at),
                     'market_count': t.market_count, 'is_current': t.is_current} for t in rows])


@app.route('/api/thesis/regenerate', methods=['POST'])
@admin
def thesis_regenerate():
    if not llm.api_key_configured():
        return error('The AI is not set up: ANTHROPIC_API_KEY is not configured on the backend.', 503)
    if not insights.thesis_inputs()[1]:
        return error(f'Not enough data yet — at least {insights.MIN_MARKETS} markets need a report for the same quarter.', 409)
    started = insights.regenerate_async(app, reason='requested')
    return jsonify({'started': started, 'job': insights.job_status()}), 202


@app.route('/api/chat', methods=['POST'])
def chat():
    body = request.json or {}
    message = (body.get('message') or '').strip()
    if not message:
        return error('Type a question first.')
    if not llm.api_key_configured():
        return error('Ask Claude is not set up: ANTHROPIC_API_KEY is not configured on the backend.', 503)
    try:
        return jsonify({'reply': insights.chat(message, body.get('history') or [])})
    except llm.LLMError as exc:
        return error(str(exc), 502)


# ── Startup ──────────────────────────────────────────────────────────────────

def init():
    db.create_all()
    try:
        for m in catalog.MARKETS:
            if not Market.query.filter_by(name=m['name']).first():
                db.session.add(Market(name=m['name'], region=m['region'], tracked=True))
        db.session.commit()
    except IntegrityError:          # another process created them at the same moment
        db.session.rollback()
    scanner.sync_targets()
    ingest.recover_interrupted()


with app.app_context():
    init()

if os.environ.get('GLENSTAR_DISABLE_SCHEDULER') != '1':
    scheduler = BackgroundScheduler(timezone='UTC')

    def scheduled_scan():
        with app.app_context():
            scanner.start_run(app, trigger='schedule')

    scheduler.add_job(scheduled_scan, 'cron', hour='6,18', minute=0, id='twice_daily_scan')
    scheduler.start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
