import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp = tempfile.mkdtemp(prefix='glenstar-test-')
os.environ['DATABASE_URL'] = f'sqlite:///{_tmp}/test.db'
os.environ['GLENSTAR_DISABLE_SCHEDULER'] = '1'
os.environ['GLENSTAR_ALLOW_PRIVATE_URLS'] = '1'
os.environ.pop('ADMIN_TOKEN', None)

import pytest  # noqa: E402

import app as app_module  # noqa: E402
from models import db  # noqa: E402


@pytest.fixture()
def app():
    with app_module.app.app_context():
        db.drop_all()
        app_module.init()
        yield app_module.app
        db.session.remove()


@pytest.fixture()
def client(app):
    return app.test_client()


def make_pdf(pages):
    """Build a real PDF whose pages contain the given lines of text."""
    import io
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    for lines in pages:
        y = 740
        for line in lines:
            c.drawString(60, y, line)
            y -= 18
        c.showPage()
    c.save()
    return buf.getvalue()


DFW_PAGES = [
    ['JLL Research', 'Dallas-Fort Worth Industrial Insight', 'Q3 2026'],
    ['Market fundamentals',
     'Total vacancy rate ended the quarter at 7.2%, down 20 basis points.',
     'Net absorption totaled 6,412,300 SF in the third quarter.',
     'Year-to-date net absorption reached 18.4 million SF.',
     'Average asking rent was $9.85 per SF per year NNN.',
     'Under construction: 24,150,000 SF',
     'Total inventory: 1,023,456,789 SF'],
]


def dfw_extraction(overrides=None, extra=None):
    """What a correct first-pass read of DFW_PAGES looks like."""
    values = [
        {'market': 'Dallas-Fort Worth', 'metric': 'vacancy_rate', 'segment': '', 'value': 7.2, 'printed': '7.2%',
         'rent_period': 'na', 'quote': 'Total vacancy rate ended the quarter at 7.2%', 'page': 2, 'note': ''},
        {'market': 'Dallas-Fort Worth', 'metric': 'net_absorption_qtr', 'segment': '', 'value': 6412300,
         'printed': '6,412,300 SF', 'rent_period': 'na',
         'quote': 'Net absorption totaled 6,412,300 SF in the third quarter.', 'page': 2, 'note': ''},
        {'market': 'Dallas-Fort Worth', 'metric': 'net_absorption_ytd', 'segment': '', 'value': 18400000,
         'printed': '18.4 million SF', 'rent_period': 'na',
         'quote': 'Year-to-date net absorption reached 18.4 million SF.', 'page': 2, 'note': ''},
        {'market': 'Dallas-Fort Worth', 'metric': 'asking_rent', 'segment': '', 'value': 9.85, 'printed': '$9.85',
         'rent_period': 'yr', 'quote': 'Average asking rent was $9.85 per SF per year NNN.', 'page': 2, 'note': 'NNN'},
        {'market': 'Dallas-Fort Worth', 'metric': 'under_construction', 'segment': '', 'value': 24150000,
         'printed': '24,150,000 SF', 'rent_period': 'na', 'quote': 'Under construction: 24,150,000 SF', 'page': 2,
         'note': ''},
    ]
    for i, patch in (overrides or {}).items():
        values[i].update(patch)
    values += extra or []
    return {'is_industrial_report': True, 'rejection_reason': '', 'brokerage': 'JLL', 'brokerage_as_printed': 'JLL',
            'report_title': 'Dallas-Fort Worth Industrial Insight', 'quarter': 'Q3 2026',
            'quarter_evidence': 'Q3 2026', 'scope': 'single_market', 'primary_market': 'Dallas-Fort Worth',
            'values': values}


@pytest.fixture()
def fake_ai(monkeypatch):
    """Replace the two Claude passes with controllable stand-ins."""
    import extraction
    import llm
    state = {'extraction': dfw_extraction(), 'audit': 'correct', 'calls': 0}

    def fake_extract(data, mime, pages, filename=''):
        state['calls'] += 1
        result = state['extraction']
        return result(pages) if callable(result) else result

    def fake_audit(data, mime, pages, quarter, values):
        verdict = state['audit']
        if verdict is None:
            raise llm.LLMError('audit unavailable')
        if callable(verdict):
            return verdict(values)
        return {i: {'index': i, 'verdict': verdict, 'corrected_value': None, 'comment': ''}
                for i in range(len(values))}

    monkeypatch.setattr(extraction, 'extract', fake_extract)
    monkeypatch.setattr(extraction, 'audit', fake_audit)
    monkeypatch.setattr(llm, 'api_key_configured', lambda: True)
    return state
