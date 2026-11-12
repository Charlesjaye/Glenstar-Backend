"""Reading broker reports: PDF text extraction and the two Claude passes
(extract, then independently audit). Nothing here decides what gets published —
that is verification.py's job."""
import base64
import io
import logging

from pypdf import PdfReader

import llm
from catalog import BROKERAGES, METRICS, METRIC_KEYS, TREND_METRICS

logger = logging.getLogger(__name__)

MAX_PDF_BYTES = 30 * 1024 * 1024
MAX_PDF_PAGES = 300


class DocumentError(Exception):
    """The file cannot be processed; the message is shown to the user."""


def pdf_pages(data):
    """Return the text of each page of a PDF. Raises DocumentError for unreadable files."""
    if not data[:1024].lstrip().startswith(b'%PDF'):
        raise DocumentError('This file is not a PDF.')
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt('')
            except Exception:
                raise DocumentError('This PDF is password-protected. Save an unprotected copy and upload that.')
        pages = []
        for page in reader.pages:
            try:
                pages.append(page.extract_text() or '')
            except Exception:
                pages.append('')
    except DocumentError:
        raise
    except Exception as exc:
        raise DocumentError(f'This PDF could not be opened ({exc.__class__.__name__}). It may be damaged.')
    if not pages:
        raise DocumentError('This PDF has no pages.')
    if len(pages) > MAX_PDF_PAGES:
        raise DocumentError(f'This PDF has {len(pages)} pages; the limit is {MAX_PDF_PAGES}.')
    return pages


def has_text_layer(pages):
    """False for scanned/image-only PDFs, where figures cannot be checked against text."""
    return sum(len(p.strip()) for p in pages) >= 200


_METRIC_LIST = '\n'.join(f'- {m["key"]}: {m["definition"]}' for m in METRICS)
_TREND_KEYS = ', '.join(TREND_METRICS)

EXTRACTION_SYSTEM = f"""You read commercial real estate brokerage research reports for an investment firm that \
develops industrial property. Your output feeds a database where every number must be exactly what the broker \
published. An investment committee relies on it, and each value you return is checked by software against the \
document text and then by a second reviewer, so precision matters far more than coverage: a missing value costs \
nothing, a wrong one is a serious problem.

What to do:
1. Decide whether this is a research report about the INDUSTRIAL property market (industrial, logistics, warehouse, \
flex). If it is an office/retail/multifamily report, a marketing brochure, or anything else, set \
is_industrial_report to false, explain why in rejection_reason, and return no values.
2. Identify the brokerage, the report title, and the reporting quarter. Take the quarter only from what the document \
says (cover, header, or the period its statistics are labelled with) — never from the file name or from today's \
date. If the document does not state a quarter, set quarter to null.
3. Extract the statistics defined below for the report's current quarter. Skip forecasts and national/US totals. \
A report may cover one market or many (for example a national table with a row per market); return values for every \
market it gives current-quarter statistics for.
4. Where the report prints the same statistic for a comparison period — the prior quarter or the same quarter a year \
earlier, typically as extra columns in the statistics table or as "up from 6.8% a year ago" — also return those, \
with period set to "prior_quarter" or "prior_year". Do this only for: {_TREND_KEYS}. These show which direction a \
market is moving, so they matter; but return one only when the report prints the actual prior figure, never by \
working backwards from a stated change.

Metrics (use exactly these keys):
{_METRIC_LIST}

Rules for each value:
- Only return a number that is printed in the document. Do not calculate, estimate, average, annualise, convert, or \
infer anything. If the report gives vacancy but not availability, return vacancy only.
- If a figure does not clearly match one of the definitions (for example a 12-month absorption figure, or a \
"direct" vacancy rate when a total rate is also given), leave it out.
- market: the market name exactly as printed. For a single-market report use the report's market name for every value.
- segment: leave empty for the market-wide total. When the report breaks a metric down by building size (for \
example "100,000-249,999 SF", "Under 50K", "500K+") or by product type (for example "Warehouse/Distribution", \
"Flex"), return a value for every row of that breakdown and copy the row label exactly as printed, including the \
size numbers — the size range is read from this label. Size breakdowns are especially useful; do not skip them. \
Do not extract submarket rows.
- period: "current" for the report quarter; "prior_quarter" or "prior_year" only as described in step 4.
- printed: the figure exactly as it appears, including symbols and units, for example "7.2%", "(1,234,567)", \
"$9.85", "24.2 MSF".
- value: the same figure as a plain number. Percentages as printed (7.2 for 7.2%). Areas in square feet \
(24.2 MSF is 24200000; a table headed "SF (000s)" showing 1,235 is 1235000). Durations in months (a lease term \
printed as "5 years" is 60). Negative figures, including those shown in parentheses, as negative numbers.
- Underwriting inputs (free rent, TI allowances, lease terms, escalations, operating expenses, taxes, cap rates, \
construction costs) appear in only some reports. Return them when the report states a specific figure for the \
market; if it gives a range ("3-4 months"), leave it out rather than choosing a number.
- rent_period: "yr" or "mo" for asking_rent, according to how the report quotes rents; "na" for every other metric.
- quote: a short verbatim passage copied character-for-character from the document that contains the printed \
figure and enough context to show what it measures — the sentence, or for a table the row label and the cells up to \
and including the figure. Do not paraphrase or tidy it.
- page: the 1-based page number of the PDF page the figure is on.
- note: optional, for anything a reader needs to interpret the figure (for example "NNN", "direct asking rent", \
"includes flex"). Otherwise empty."""

EXTRACTION_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'required': ['is_industrial_report', 'rejection_reason', 'brokerage', 'brokerage_as_printed', 'report_title',
                 'quarter', 'quarter_evidence', 'scope', 'primary_market', 'values'],
    'properties': {
        'is_industrial_report': {'type': 'boolean'},
        'rejection_reason': {'type': 'string'},
        'brokerage': {'type': 'string', 'enum': BROKERAGES},
        'brokerage_as_printed': {'type': 'string'},
        'report_title': {'type': 'string'},
        'quarter': {'anyOf': [{'type': 'string', 'description': 'Format: Q3 2026'}, {'type': 'null'}]},
        'quarter_evidence': {'type': 'string', 'description': 'Verbatim text from the document that states the quarter.'},
        'scope': {'type': 'string', 'enum': ['single_market', 'multi_market']},
        'primary_market': {'type': 'string', 'description': 'Market name as printed; empty for multi-market reports.'},
        'values': {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'required': ['market', 'metric', 'segment', 'period', 'value', 'printed', 'rent_period', 'quote', 'page', 'note'],
                'properties': {
                    'market': {'type': 'string'},
                    'metric': {'type': 'string', 'enum': METRIC_KEYS},
                    'segment': {'type': 'string'},
                    'period': {'type': 'string', 'enum': ['current', 'prior_quarter', 'prior_year']},
                    'value': {'type': 'number'},
                    'printed': {'type': 'string'},
                    'rent_period': {'type': 'string', 'enum': ['yr', 'mo', 'na']},
                    'quote': {'type': 'string'},
                    'page': {'type': 'integer'},
                    'note': {'type': 'string'},
                },
            },
        },
    },
}

AUDIT_SYSTEM = f"""You are the second, independent reviewer of statistics that were transcribed from a commercial \
real estate brokerage report into a database used for investment decisions. You are given the report and a numbered \
list of transcribed values. For each one, look at the report yourself and decide whether the database entry is right.

Mark an item "correct" only if all of these hold: the figure is printed in the report on or near the cited page; it \
is for the stated market (and segment, if any); it measures what the metric says, per the definitions below; it is \
for the report's current quarter, not a prior period or a forecast; and the stored number, sign and unit match what \
is printed (areas are stored in square feet; a rent's period is yr or mo as the report quotes it).

Mark it "incorrect" if you can see the figure but any of those fail — say what is wrong, and give the right number \
in corrected_value if the report prints it. Mark it "cannot_find" if you cannot locate the figure in the report.

Be strict. A wrong "correct" lets a bad number reach an investment committee; a wrong "incorrect" only sends the \
value to a person to double-check.

Metric definitions:
{_METRIC_LIST}"""

AUDIT_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'required': ['items'],
    'properties': {
        'items': {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'required': ['index', 'verdict', 'corrected_value', 'comment'],
                'properties': {
                    'index': {'type': 'integer'},
                    'verdict': {'type': 'string', 'enum': ['correct', 'incorrect', 'cannot_find']},
                    'corrected_value': {'anyOf': [{'type': 'number'}, {'type': 'null'}]},
                    'comment': {'type': 'string'},
                },
            },
        },
    },
}


def _document_block(data, mime, pages):
    if mime == 'application/pdf':
        return {'type': 'document',
                'source': {'type': 'base64', 'media_type': 'application/pdf',
                           'data': base64.standard_b64encode(data).decode('ascii')}}
    return {'type': 'document',
            'source': {'type': 'text', 'media_type': 'text/plain', 'data': '\n\n'.join(pages)}}


def extract(data, mime, pages, filename=''):
    """First pass: read the report. Returns the dict described by EXTRACTION_SCHEMA."""
    return llm.complete(
        system=EXTRACTION_SYSTEM,
        messages=[{'role': 'user', 'content': [
            _document_block(data, mime, pages),
            {'type': 'text', 'text': 'Extract the industrial market statistics from this report.'},
        ]}],
        schema=EXTRACTION_SCHEMA,
        effort='high',
        max_tokens=64000,
    )


def audit(data, mime, pages, quarter, values):
    """Second pass: an independent check of each extracted value against the report.
    Returns {index: {'verdict', 'corrected_value', 'comment'}}."""
    lines = []
    for i, v in enumerate(values):
        seg = f' | segment: {v["segment"]}' if v.get('segment') else ''
        period = f' per {v["period"]}' if v.get('period') in ('yr', 'mo') else ''
        lines.append(f'{i}. market: {v["market_printed"]}{seg} | metric: {v["metric"]} | quarter: {v["quarter"]} | '
                     f'stored value: {v["value"]:g} ({v["unit"]}{period}) | page: {v["page"]}')
    result = llm.complete(
        system=AUDIT_SYSTEM,
        messages=[{'role': 'user', 'content': [
            _document_block(data, mime, pages),
            {'type': 'text', 'text': f'Report quarter: {quarter}\n\nTranscribed values to check:\n' + '\n'.join(lines)},
        ]}],
        schema=AUDIT_SCHEMA,
        effort='high',
        max_tokens=32000,
    )
    return {item['index']: item for item in result.get('items', [])}
