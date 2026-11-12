"""AI commentary built strictly on published report data: the investment thesis
and the Ask Claude chat. The model is given only verified figures, each tagged
with its source, and is told not to bring in any outside statistics. The
thesis text is then checked in code: any figure in it that cannot be matched to
the supplied data is recorded and shown as a warning."""
import hashlib
import json
import logging
import re
import threading
from datetime import datetime

import catalog
import data
import llm
import quarters
from models import Document, Job, Thesis, db

logger = logging.getLogger(__name__)

MIN_MARKETS = 3


def fmt_value(metric, v):
    unit, value = v['unit'], v['value']
    if unit == 'percent':
        return f'{value:g}%'
    if unit == 'sf':
        return f'{value:,.0f} SF'
    if metric == 'asking_rent':
        return f'${value:,.2f}/SF/{v.get("period") or "yr"}'
    return f'${value:,.2f}/SF'


def market_line(m, with_segments=False):
    parts = [f'{m["market"]} ({m["region"] or "region n/a"}) | {m["quarter"]}']
    for key in catalog.METRIC_KEYS:
        v = m['metrics'].get(key)
        if v:
            parts.append(f'{catalog.METRIC_BY_KEY[key]["label"]}: {fmt_value(key, v)} [{v["doc"]["source"]}, p.{v["page"]}]')
    line = ' | '.join(parts)
    if with_segments:
        for seg in m['segments']:
            seg_parts = [f'{catalog.METRIC_BY_KEY[k]["label"]}: {fmt_value(k, v)} [{v["doc"]["source"]}]'
                         for k, v in seg['metrics'].items()]
            line += f'\n    - {seg["segment"]}: ' + '; '.join(seg_parts)
    return line


def thesis_inputs(snapshot=None):
    """The quarter the thesis covers and the markets that have data for it.
    The thesis uses the newest quarter that at least MIN_MARKETS markets report."""
    snapshot = snapshot or data.build_snapshot()
    by_quarter = {}
    for m in snapshot['markets']:
        if any(k in m['metrics'] for k in ('vacancy_rate', 'asking_rent', 'net_absorption_qtr', 'net_absorption_ytd')):
            by_quarter.setdefault(m['quarter'], []).append(m)
    eligible = [q for q, ms in by_quarter.items() if len(ms) >= MIN_MARKETS]
    if not eligible:
        return None, [], ''
    quarter = max(eligible, key=quarters.index)
    markets = by_quarter[quarter]
    lines = [market_line(m) for m in markets]
    fingerprint = hashlib.sha256('\n'.join(lines).encode()).hexdigest()
    return quarter, markets, fingerprint


THESIS_SYSTEM = """You are a senior industrial real estate investment analyst at Glenstar Properties, a developer \
of industrial buildings. You write the firm's quarterly market thesis for the investment committee.

You are given a table of statistics transcribed from brokerage research reports. Each figure is tagged with the \
brokerage it came from. This table is the only factual source you may use.

Accuracy rules — the committee checks every number against the source reports:
- Every statistic you write must appear in the table, for that market, written the same way (you may shorten \
square feet to millions, for example 3,200,000 SF as 3.2 MSF, and round to one decimal place).
- Do not introduce any figure from memory or general knowledge: no national averages, interest rates, tariff rates, \
construction costs, forecasts, or historical values that are not in the table.
- Do not calculate new figures (differences, ratios, totals, averages, spreads). Compare markets in words instead \
("the lowest vacancy in the set", "roughly double").
- If a market lacks a metric, say it is not reported rather than estimating it.
- Different brokerages define markets and methodology differently. When comparing markets sourced from different \
brokerages, keep the comparison directional.

What to produce:
- summary: four or five paragraphs of analysis for a development-focused investor: where fundamentals are strongest \
and weakest, what the supply pipeline implies, and what Glenstar should do about it. Separate paragraphs with a \
blank line. Plain prose, no headings or bullet points.
- rankings: every market in the table, each with a 0-100 score reflecting development attractiveness on the \
reported data (tight vacancy, positive absorption, manageable pipeline, rent level and growth), a tier, a one-sentence \
headline, two or three sentences of detail, and three to five scored factors. Markets with thin data should score \
conservatively and say so. Scores are your judgement; make clear in the wording that they are derived from the \
reported figures.
- risks: three to six risks that follow from the data in the table (for example markets where the pipeline is large \
relative to absorption, or where vacancy is elevated). Do not cite outside events or statistics."""


def _thesis_schema(market_names):
    return {
        'type': 'object', 'additionalProperties': False,
        'required': ['summary', 'rankings', 'risks'],
        'properties': {
            'summary': {'type': 'string'},
            'rankings': {'type': 'array', 'items': {
                'type': 'object', 'additionalProperties': False,
                'required': ['market', 'score', 'tier', 'headline', 'detail', 'factors'],
                'properties': {
                    'market': {'type': 'string', 'enum': market_names},
                    'score': {'type': 'integer'},
                    'tier': {'type': 'string', 'enum': ['Primary', 'Secondary', 'Caution', 'Avoid']},
                    'headline': {'type': 'string'},
                    'detail': {'type': 'string'},
                    'factors': {'type': 'array', 'items': {
                        'type': 'object', 'additionalProperties': False,
                        'required': ['name', 'score', 'note'],
                        'properties': {'name': {'type': 'string'}, 'score': {'type': 'integer'},
                                       'note': {'type': 'string'}},
                    }},
                },
            }},
            'risks': {'type': 'array', 'items': {
                'type': 'object', 'additionalProperties': False,
                'required': ['level', 'title', 'detail'],
                'properties': {'level': {'type': 'string', 'enum': ['high', 'medium', 'low']},
                               'title': {'type': 'string'}, 'detail': {'type': 'string'}},
            }},
        },
    }


_NUMBER = re.compile(r'(?<![A-Za-z\d.,])\$?(\d[\d,]*(?:\.\d+)?)')


def allowed_numbers(markets):
    """Every numeric form in which a supplied figure may legitimately appear in the commentary."""
    allowed = set()

    def add(x):
        for digits in (0, 1, 2):
            allowed.add(round(abs(x), digits))

    for m in markets:
        for v in m['metrics'].values():
            value = v['value']
            add(value)
            if v['unit'] == 'sf':
                add(value / 1e6)
                add(value / 1e3)
            if v.get('annualized') is not None:
                add(v['annualized'])
    return allowed


def unverified_figures(text, markets, extra_allowed=()):
    """Figures in `text` that do not correspond to any supplied statistic.
    Small counts, years and quarter numbers are ignored."""
    allowed = allowed_numbers(markets) | {float(x) for x in extra_allowed}
    flagged = []
    for match in _NUMBER.finditer(text or ''):
        token = match.group(1)
        try:
            number = float(token.replace(',', ''))
        except ValueError:
            continue
        following = text[match.end():match.end() + 12].lower()
        is_stat = match.group(0).startswith('$') or '.' in token or re.match(
            r'\s*(%|percent|bps|basis|msf|million|sf\b|square|/sf|psf)', following)
        if not is_stat:
            if 2000 <= number <= 2100 or number <= 12:
                continue                          # years, quarters, small counts ("top 5 markets")
            if float(number).is_integer() and number <= 100 and not re.match(r'\s*(%|percent)', following):
                continue                          # scores and ranks
        if not any(abs(number - a) < 1e-9 for a in allowed):
            snippet = text[max(0, match.start() - 40):match.end() + 25].replace('\n', ' ').strip()
            figure = match.group(0) + ('%' if following.lstrip().startswith('%') else '')
            flagged.append({'figure': figure, 'context': snippet})
    seen, unique = set(), []
    for f in flagged:
        if f['figure'] not in seen:
            seen.add(f['figure'])
            unique.append(f)
    return unique


def generate_thesis():
    """Generate and store a thesis from the current published data.
    Returns the Thesis, or None when there is not yet enough data."""
    snapshot = data.build_snapshot()
    quarter, markets, fingerprint = thesis_inputs(snapshot)
    if not markets:
        return None
    names = [m['market'] for m in markets]
    table = '\n'.join(market_line(m) for m in markets)
    user = (f'Reporting quarter: {quarter}\nMarkets with reported data: {len(markets)}\n\n'
            f'Statistics (the only figures you may use):\n{table}')

    result, warnings = None, []
    messages = [{'role': 'user', 'content': user}]
    for attempt in range(2):
        result = llm.complete(system=THESIS_SYSTEM, messages=messages, schema=_thesis_schema(names),
                              effort='high', max_tokens=32000)
        scores = [r.get('score', 0) for r in result.get('rankings', [])]
        text = '\n'.join([result.get('summary', '')]
                         + [f'{r.get("headline", "")} {r.get("detail", "")} '
                            + ' '.join(f.get('note', '') for f in r.get('factors', []))
                            for r in result.get('rankings', [])]
                         + [f'{r.get("title", "")} {r.get("detail", "")}' for r in result.get('risks', [])])
        warnings = unverified_figures(text, markets, extra_allowed=scores)
        if not warnings or attempt == 1:
            break
        messages = [
            {'role': 'user', 'content': user},
            {'role': 'assistant', 'content': json.dumps(result)},
            {'role': 'user', 'content':
                'These figures in your draft do not appear in the statistics table: '
                + ', '.join(w['figure'] for w in warnings[:30])
                + '. Rewrite the thesis so that every figure comes from the table exactly; replace anything else '
                  'with a comparison in words.'},
        ]

    by_name = {m['market']: m for m in markets}
    rankings = []
    for r in sorted(result.get('rankings', []), key=lambda x: -x.get('score', 0)):
        m = by_name.get(r['market'])
        if not m:
            continue
        rankings.append({
            'rank': len(rankings) + 1, 'market': r['market'], 'region': m['region'], 'quarter': m['quarter'],
            'source': m['primary_source'], 'score': max(0, min(100, int(r.get('score', 0)))), 'tier': r.get('tier'),
            'headline': r.get('headline', ''), 'detail': r.get('detail', ''),
            'factors': [{'name': f.get('name', ''), 'score': max(1, min(5, int(f.get('score', 3)))),
                         'note': f.get('note', '')} for f in r.get('factors', [])],
        })

    Thesis.query.update({'is_current': False})
    thesis = Thesis(
        quarter=quarter, summary=result.get('summary', ''), rankings_json=json.dumps(rankings),
        risks_json=json.dumps(result.get('risks', [])), warnings_json=json.dumps(warnings),
        market_count=len(markets),
        document_count=len({d['id'] for m in markets for d in m['documents']}),
        data_fingerprint=fingerprint, is_current=True,
    )
    db.session.add(thesis)
    db.session.commit()
    return thesis


def _set_job(status, detail=None):
    job = db.session.get(Job, 'thesis') or Job(name='thesis')
    job.status, job.detail, job.updated_at = status, detail, datetime.utcnow()
    db.session.add(job)
    db.session.commit()


def job_status():
    job = db.session.get(Job, 'thesis')
    if not job:
        return {'status': 'idle', 'detail': None}
    # A job left "running" by a restart should not block the UI forever.
    if job.status == 'running' and (datetime.utcnow() - job.updated_at).total_seconds() > 900:
        return {'status': 'failed', 'detail': 'Thesis generation was interrupted. Try again.'}
    return {'status': job.status, 'detail': job.detail}


def regenerate_async(app, reason=''):
    """Start thesis generation in the background. Returns False if it is already running."""
    with app.app_context():
        if job_status()['status'] == 'running':
            return False
        _set_job('running', reason)

    def work():
        with app.app_context():
            try:
                thesis = generate_thesis()
                _set_job('idle', None if thesis else f'Not enough data yet — at least {MIN_MARKETS} markets need a report.')
            except llm.LLMError as exc:
                db.session.rollback()
                _set_job('failed', str(exc))
            except Exception:
                logger.exception('Thesis generation failed')
                db.session.rollback()
                _set_job('failed', 'Thesis generation failed unexpectedly.')
            finally:
                db.session.remove()

    threading.Thread(target=work, name='thesis', daemon=True).start()
    return True


def maybe_regenerate(app):
    """After new reports are read: refresh the thesis once nothing else is being processed
    and the underlying data has actually changed."""
    with app.app_context():
        if not llm.api_key_configured():
            return False
        if Document.query.filter(Document.status.in_(('queued', 'processing'))).count():
            return False
        _, markets, fingerprint = thesis_inputs()
        current = Thesis.query.filter_by(is_current=True).first()
        if not markets or (current and current.data_fingerprint == fingerprint):
            return False
    return regenerate_async(app, reason='new report data')


CHAT_SYSTEM = """You are the research assistant inside Glenstar Properties' industrial market intelligence \
platform. Glenstar develops industrial buildings; the people asking are its investment and development team.

Below is the platform's database: statistics transcribed from brokerage research reports, each tagged with the \
brokerage and page it came from. It is your only source of market facts.

- Answer with specific figures from the database and name the brokerage and quarter they come from, for example \
"7.2% (JLL, Q3 2026)".
- If the database does not contain what is asked — a market with no report, a metric the broker did not publish, \
construction costs, cap rates, forecasts — say plainly that it is not in the reports on file, and suggest adding the \
relevant broker report. Never fill a gap from memory or general knowledge, and never estimate.
- Do not calculate new statistics beyond simple, clearly-labelled comparisons the user asks for, and show the two \
source figures when you do.
- Markets sourced from different brokerages are not measured identically; say so when a comparison depends on it.
- Some markets may have older data than others; mention the quarter when it differs from the latest one.
- Be direct and concise. Give a recommendation when asked for one, grounded in the reported figures. Industrial \
property only."""


def chat(message, history):
    snapshot = data.build_snapshot()
    if snapshot['markets']:
        table = '\n'.join(market_line(m, with_segments=True) for m in snapshot['markets'])
    else:
        table = '(The database is empty — no broker reports have been added yet.)'
    thesis = Thesis.query.filter_by(is_current=True).first()
    context = f'Latest published quarter: {snapshot["expected_quarter"]}\n\nDATABASE\n{table}'
    if thesis:
        context += f'\n\nCURRENT INVESTMENT THESIS ({thesis.quarter})\n{thesis.summary}'
    messages = [{'role': h['role'], 'content': str(h['content'])}
                for h in (history or [])[-12:]
                if h.get('role') in ('user', 'assistant') and h.get('content')]
    while messages and messages[0]['role'] != 'user':
        messages.pop(0)
    messages.append({'role': 'user', 'content': message})
    return llm.complete(system=f'{CHAT_SYSTEM}\n\n{context}', messages=messages, effort='low', max_tokens=6000)
