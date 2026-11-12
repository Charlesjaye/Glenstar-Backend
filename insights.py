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
    if unit == 'months':
        return f'{value:g} months'
    if metric == 'asking_rent':
        return f'${value:,.2f}/SF/{v.get("period") or "yr"}'
    return f'${value:,.2f}/SF'


def _fmt_signal(sig):
    value, unit = sig['value'], sig['unit']
    if unit == 'bps':
        return f'{value:+d} bps'
    if unit == 'percent':
        return f'{value:+g}%' if sig['key'] != 'pipeline_share' else f'{value:g}%'
    if unit == 'sf':
        return f'{value:+,.0f} SF'
    return f'{value:g}'


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


def market_block(m, with_segments=False):
    """A market's reported figures plus the direction-of-travel indicators calculated from them."""
    lines = [market_line(m, with_segments)]
    earlier = []
    for key in catalog.TREND_METRICS:
        v = m['metrics'].get(key)
        if not v:
            continue
        label = catalog.METRIC_BY_KEY[key]['label']
        for span in ('previous', 'year_ago'):
            if v.get(span):
                prior = {**v, 'value': v[span]['value']}
                earlier.append(f'{label} in {v[span]["quarter"]}: {fmt_value(key, prior)}')
    if earlier:
        lines.append('    Earlier figures (same broker): ' + '; '.join(earlier))
    signals = m.get('trend', {}).get('signals') or []
    if signals:
        lines.append('    Calculated indicators: ' + '; '.join(f'{s["label"]}: {_fmt_signal(s)}' for s in signals))
    direction = m.get('trend', {}).get('direction', 'unknown')
    lines.append(f'    Calculated direction of travel: {direction}'
                 + ('' if m.get('trend', {}).get('has_history') else ' (no earlier-period figures on file yet)'))
    return '\n'.join(lines)


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
    lines = [market_block(m) for m in markets]
    fingerprint = hashlib.sha256('\n'.join(lines).encode()).hexdigest()
    return quarter, markets, fingerprint


THESIS_SYSTEM = """You are a senior industrial real estate investment analyst at Glenstar Properties, a developer \
of industrial buildings. You write the firm's market thesis for the investment committee.

The committee's question is not "which market is best today" but "where is the puck going": which markets will be \
the strongest places to own and deliver industrial product two to three years from now. A building Glenstar starts \
today delivers and leases into that future market, not today's. So the markets that matter most are the ones whose \
fundamentals are moving in the right direction and that have room left to run — the next big market — rather than \
markets that are already tight, expensive and fully priced. A market with unimpressive figures today but clearly \
improving ones deserves more attention than a strong market that has started to slip.

You are given, for each market: statistics transcribed from brokerage research reports (each tagged with its \
brokerage); earlier-period figures from the same broker where a report printed them; and indicators the platform \
calculated from those figures (changes in vacancy, rent and pipeline, pipeline as a share of inventory, absorption \
against deliveries). This is the only factual source you may use.

How to read direction of travel:
- Vacancy falling, absorption positive and ahead of deliveries, and a shrinking pipeline point to a market \
tightening over the next two to three years. The reverse points to softening.
- A large pipeline relative to inventory is supply that will hit the market inside the horizon; weigh it against \
the pace of absorption.
- Rent level matters as room to run: a market with lower rents and improving fundamentals has more upside than one \
already at premium rents.
- Where no earlier-period figures are on file for a market, you cannot see its direction. Say so, score it \
conservatively, and set confidence to low. Do not infer a trend from a single quarter's level.

Accuracy rules — the committee checks every number against the source reports:
- Every statistic you write must appear in the data for that market, written the same way (you may shorten square \
feet to millions, for example 3,200,000 SF as 3.2 MSF, and round to one decimal place).
- Do not introduce any figure from memory or general knowledge: no national averages, interest rates, tariffs, \
population or job growth numbers, construction costs or forecasts that are not in the data.
- Do not calculate new figures. The calculated indicators are already provided; use those, and otherwise compare \
in words.
- You may draw on general, widely known structural context in words only (for example that a market is a port, an \
inland hub or a reshoring destination) when it helps explain why a trend may persist — but mark it as context, not \
data, and never attach a number to it.
- This is a judgement about the future made from current data. Write it as a reasoned view with its conditions, \
not as a prediction of specific future values. Do not state future vacancy rates, rents or dates as facts.
- Different brokerages define markets differently; keep cross-broker comparisons directional.

What to produce:
- summary: four or five paragraphs. Lead with where the puck is going: the markets positioned to be the next \
leaders and why the data says so. Then the established markets and whether they still have room to run, the \
markets that are slipping, and what Glenstar should do now — where to pursue land and starts for delivery in two \
to three years, and where to wait. Plain prose, blank line between paragraphs, no headings or bullets.
- rankings: every market, ordered by how attractive it should be two to three years out. For each: \
score (0-100, the forward-looking trajectory score, not a rating of today), \
outlook ("Emerging" = improving with room to run, the next-big-market candidates; "Established" = strong today \
and holding; "Watch" = mixed or not enough history to call; "Deteriorating" = moving the wrong way), \
direction (improving / stable / weakening / unclear), \
confidence (high / medium / low, reflecting how much trend evidence is on file), \
a one-sentence headline, a two-to-three-sentence thesis for the two-to-three-year view, \
three to five scored signals each tied to a figure or indicator in the data, \
and watch: the one or two data points that would change your view if they moved.
- risks: three to six risks to the forward view that follow from the data (for example supply still to deliver, \
absorption that has turned negative, or thin trend history)."""


def _thesis_schema(market_names):
    return {
        'type': 'object', 'additionalProperties': False,
        'required': ['summary', 'rankings', 'risks'],
        'properties': {
            'summary': {'type': 'string'},
            'rankings': {'type': 'array', 'items': {
                'type': 'object', 'additionalProperties': False,
                'required': ['market', 'score', 'outlook', 'direction', 'confidence', 'headline', 'thesis',
                             'signals', 'watch'],
                'properties': {
                    'market': {'type': 'string', 'enum': market_names},
                    'score': {'type': 'integer'},
                    'outlook': {'type': 'string', 'enum': ['Emerging', 'Established', 'Watch', 'Deteriorating']},
                    'direction': {'type': 'string', 'enum': ['improving', 'stable', 'weakening', 'unclear']},
                    'confidence': {'type': 'string', 'enum': ['high', 'medium', 'low']},
                    'headline': {'type': 'string'},
                    'thesis': {'type': 'string'},
                    'signals': {'type': 'array', 'items': {
                        'type': 'object', 'additionalProperties': False,
                        'required': ['name', 'score', 'note'],
                        'properties': {'name': {'type': 'string'}, 'score': {'type': 'integer'},
                                       'note': {'type': 'string'}},
                    }},
                    'watch': {'type': 'string'},
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
            for span in ('previous', 'year_ago'):
                if v.get(span):
                    add(v[span]['value'])
                    if v['unit'] == 'sf':
                        add(v[span]['value'] / 1e6)
        for sig in (m.get('trend') or {}).get('signals', []):
            add(sig['value'])
            if sig['unit'] == 'sf':
                add(sig['value'] / 1e6)
                add(sig['value'] / 1e3)
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
    table = '\n\n'.join(market_block(m) for m in markets)
    horizon = f'{int(quarter[3:]) + 2}–{int(quarter[3:]) + 3}'
    user = (f'Latest reported quarter: {quarter}\nHorizon the committee cares about: {horizon}\n'
            f'Markets with reported data: {len(markets)}\n\nData (the only figures you may use):\n\n{table}')

    result, warnings = None, []
    messages = [{'role': 'user', 'content': user}]
    for attempt in range(2):
        result = llm.complete(system=THESIS_SYSTEM, messages=messages, schema=_thesis_schema(names),
                              effort='high', max_tokens=32000)
        scores = [r.get('score', 0) for r in result.get('rankings', [])]
        text = '\n'.join([result.get('summary', '')]
                         + [f'{r.get("headline", "")} {r.get("thesis", "")} {r.get("watch", "")} '
                            + ' '.join(f.get('note', '') for f in r.get('signals', []))
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
            'source': m['primary_source'], 'score': max(0, min(100, int(r.get('score', 0)))),
            'outlook': r.get('outlook'), 'direction': r.get('direction'), 'confidence': r.get('confidence'),
            'headline': r.get('headline', ''), 'thesis': r.get('thesis', ''), 'watch': r.get('watch', ''),
            'signals': [{'name': f.get('name', ''), 'score': max(1, min(5, int(f.get('score', 3)))),
                         'note': f.get('note', '')} for f in r.get('signals', [])],
            'calculated_direction': m['trend']['direction'],
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
- The team thinks two to three years ahead: when asked which markets are attractive, weigh direction of travel \
(the earlier-period figures and calculated indicators in the database) as well as today's levels, and say when a \
market has no trend history on file. Give views about the future as reasoned judgement, not as forecast numbers.
- Be direct and concise. Give a recommendation when asked for one, grounded in the reported figures. Industrial \
property only."""


def chat(message, history):
    snapshot = data.build_snapshot()
    if snapshot['markets']:
        table = '\n\n'.join(market_block(m, with_segments=True) for m in snapshot['markets'])
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
