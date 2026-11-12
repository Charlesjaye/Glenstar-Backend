"""Database models. Table names are prefixed `gs_` so they never collide with the
tables created by earlier versions of this app (those are simply left unused)."""
import json
from datetime import datetime

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

# Value statuses that are allowed to appear on the site.
PUBLISHED = ('verified', 'confirmed', 'edited', 'manual')


def utcnow():
    return datetime.utcnow()


class Market(db.Model):
    __tablename__ = 'gs_market'
    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(120), unique=True, nullable=False)
    region     = db.Column(db.String(60), default='')
    tracked    = db.Column(db.Boolean, default=True)     # appears in the coverage checklist
    created_at = db.Column(db.DateTime, default=utcnow)


class Document(db.Model):
    """One broker report: an uploaded PDF, or a file/page found by the scanner."""
    __tablename__ = 'gs_document'
    id            = db.Column(db.Integer, primary_key=True)
    sha256        = db.Column(db.String(64), unique=True, nullable=False)
    origin        = db.Column(db.String(20), default='upload')    # upload | link | scan
    filename      = db.Column(db.String(300))
    mime          = db.Column(db.String(80), default='application/pdf')
    size          = db.Column(db.Integer, default=0)
    content       = db.deferred(db.Column(db.LargeBinary))        # the original file
    pages_json    = db.deferred(db.Column(db.Text))               # extracted text, one entry per page
    page_count    = db.Column(db.Integer, default=0)
    has_text      = db.Column(db.Boolean, default=True)           # False = scanned image PDF
    report_url    = db.Column(db.String(800))
    # Identity, read from the document itself
    source        = db.Column(db.String(60))
    title         = db.Column(db.String(300))
    quarter       = db.Column(db.String(10))
    quarter_idx   = db.Column(db.Integer, index=True)
    quarter_manual = db.Column(db.Boolean, default=False)          # quarter was set by a person
    scope         = db.Column(db.String(20))                      # single_market | multi_market
    primary_market = db.Column(db.String(120))
    # Processing
    status        = db.Column(db.String(20), default='queued', index=True)
    # queued | processing | ready | needs_review | failed | rejected
    error         = db.Column(db.Text)
    model         = db.Column(db.String(60))
    created_at    = db.Column(db.DateTime, default=utcnow)
    queued_at     = db.Column(db.DateTime, default=utcnow)
    processed_at  = db.Column(db.DateTime)

    values = db.relationship('MetricValue', backref='document', cascade='all, delete-orphan', lazy='select')

    def pages(self):
        return json.loads(self.pages_json or '[]')


class MetricValue(db.Model):
    """One number read from one document, with the evidence for it."""
    __tablename__ = 'gs_value'
    id            = db.Column(db.Integer, primary_key=True)
    document_id   = db.Column(db.Integer, db.ForeignKey('gs_document.id'), nullable=False, index=True)
    market        = db.Column(db.String(120), index=True)          # canonical market name
    market_printed = db.Column(db.String(160))                     # as written in the report
    quarter       = db.Column(db.String(10))
    quarter_idx   = db.Column(db.Integer, index=True)
    metric        = db.Column(db.String(40), index=True)
    segment       = db.Column(db.String(120), default='')          # '' = market total; else size band / product type as printed
    value         = db.Column(db.Float)
    unit          = db.Column(db.String(20))                       # percent | sf | usd_psf
    period        = db.Column(db.String(4))                        # yr | mo (rents only)
    printed       = db.Column(db.String(120))                      # the figure exactly as printed
    quote         = db.Column(db.Text)                             # verbatim surrounding text
    page          = db.Column(db.Integer)
    note          = db.Column(db.String(300))
    status        = db.Column(db.String(20), default='needs_review', index=True)
    # verified | needs_review | confirmed | edited | manual | rejected
    checks_json   = db.Column(db.Text)                             # [{check, ok, detail}]
    original_value = db.Column(db.Float)                           # set when a person edits the value
    reviewed_at   = db.Column(db.DateTime)

    def checks(self):
        return json.loads(self.checks_json or '[]')


class ScanTarget(db.Model):
    """A broker web page the scanner checks for new reports."""
    __tablename__ = 'gs_scan_target'
    __table_args__ = (db.UniqueConstraint('source', 'market', name='uq_scan_target_source_market'),)
    id           = db.Column(db.Integer, primary_key=True)
    source       = db.Column(db.String(60))
    market       = db.Column(db.String(120))
    url          = db.Column(db.String(800))
    last_checked = db.Column(db.DateTime)
    last_result  = db.Column(db.String(30))
    # new_report | no_change | blocked | not_found | no_report_found | error | needs_api_key
    http_status  = db.Column(db.Integer)
    detail       = db.Column(db.String(400))
    last_document_id = db.Column(db.Integer)


class ScanRun(db.Model):
    __tablename__ = 'gs_scan_run'
    id          = db.Column(db.Integer, primary_key=True)
    trigger     = db.Column(db.String(20))          # schedule | manual
    started_at  = db.Column(db.DateTime, default=utcnow)
    finished_at = db.Column(db.DateTime)
    status      = db.Column(db.String(20), default='running')   # running | done | failed
    total       = db.Column(db.Integer, default=0)
    checked     = db.Column(db.Integer, default=0)
    new_documents = db.Column(db.Integer, default=0)
    blocked     = db.Column(db.Integer, default=0)


class Thesis(db.Model):
    __tablename__ = 'gs_thesis'
    id            = db.Column(db.Integer, primary_key=True)
    quarter       = db.Column(db.String(10))
    generated_at  = db.Column(db.DateTime, default=utcnow)
    summary       = db.Column(db.Text)
    rankings_json = db.Column(db.Text)
    risks_json    = db.Column(db.Text)
    warnings_json = db.Column(db.Text)       # figures in the commentary that could not be matched to report data
    market_count  = db.Column(db.Integer, default=0)
    document_count = db.Column(db.Integer, default=0)
    data_fingerprint = db.Column(db.String(64))
    is_current    = db.Column(db.Boolean, default=True)


class Job(db.Model):
    """Tiny status record for background work the UI polls (thesis generation)."""
    __tablename__ = 'gs_job'
    name       = db.Column(db.String(40), primary_key=True)
    status     = db.Column(db.String(20), default='idle')   # idle | running | failed
    detail     = db.Column(db.Text)
    updated_at = db.Column(db.DateTime, default=utcnow)
