"""Read-side queries: what the site displays. Only published values (verified
automatically, or confirmed/edited/entered by a person) are ever returned here.

Anything this module *calculates* from report figures (changes between quarters,
pipeline as a share of inventory, the trend signal) is returned separately from
the figures themselves and labelled as calculated, with the inputs it used."""
import json
import os
from collections import defaultdict

import catalog
import quarters
import sizes
from models import PUBLISHED, Assumption, Document, Market, MetricValue, ScanTarget, db

_HERE = os.path.dirname(os.path.abspath(__file__))


def value_dict(v, doc):
    out = {
        'id': v.id, 'metric': v.metric, 'segment': v.segment or '', 'value': v.value, 'unit': v.unit,
        'period': v.period, 'printed': v.printed, 'quote': v.quote, 'page': v.page, 'note': v.note or '',
        'status': v.status, 'quarter': v.quarter, 'market': v.market,
        'doc': {'id': doc.id, 'source': doc.source, 'title': doc.title or doc.filename, 'origin': doc.origin,
                'quarter': doc.quarter},
    }
    if v.period_offset:
        out['restated'] = True          # a comparison figure printed in a later report
    if v.metric == 'asking_rent' and v.period == 'mo':
        out['annualized'] = round(v.value * 12, 4)
    return out


def _published():
    return (db.session.query(MetricValue, Document)
            .join(Document, MetricValue.document_id == Document.id)
            .filter(MetricValue.status.in_(PUBLISHED), Document.status.in_(('ready', 'needs_review')))
            .all())


def _recency(doc):
    return doc.processed_at or doc.created_at


def _pick(candidates, primary_doc_id):
    """Choose one value for a market/quarter/metric. Figures a report gives for its own quarter
    beat comparison figures restated in a later report; then the primary report; then the most
    recently read. Values are never averaged or blended."""
    return max(candidates, key=lambda c: (not c[0].period_offset, c[1].id == primary_doc_id, _recency(c[1]), c[0].id))


def _annual(v):
    """Rent on an annual basis, for comparisons only."""
    return v.value * 12 if v.metric == 'asking_rent' and v.period == 'mo' else v.value


def _comparison(current, by_quarter_metrics, q_idx, metric):
    """The same metric one period back, from the same broker — preferring the figure the
    current report itself prints for that period."""
    v, d = current
    earlier = by_quarter_metrics.get(q_idx, {}).get(metric)
    if not earlier:
        return None
    same_doc = [c for c in earlier if c[1].id == d.id]
    same_source = [c for c in earlier if c[1].source == d.source]
    pool = same_doc or same_source
    if not pool:
        return None
    pv, pd = max(pool, key=lambda c: (not c[0].period_offset, _recency(c[1])))
    if pv.unit != v.unit or (pv.period or 'yr') != (v.period or 'yr'):
        return None
    return pv, pd


def _segment_entry(label, rows, primary_id):
    """One size band / product type row with every metric the report gives for it."""
    first = rows[0][0]
    metrics = {}
    by_metric = defaultdict(list)
    for v, d in rows:
        by_metric[v.metric].append((v, d))
    for metric, cands in by_metric.items():
        v, d = _pick(cands, primary_id)
        metrics[metric] = value_dict(v, d)
    kind = first.segment_kind
    size_min, size_max = first.size_min, first.size_max
    if kind is None:                                   # rows stored before size parsing existed
        shape = sizes.classify(label)
        kind, size_min, size_max = shape['kind'], shape['size_min'], shape['size_max']
    bands, fit = sizes.bands_for(size_min, size_max) if kind == 'size' else ([], None)
    return {'segment': label, 'kind': kind, 'size_min': size_min, 'size_max': size_max,
            'range': sizes.format_range(size_min, size_max) if kind == 'size' else '',
            'bands': bands, 'fit': fit, 'metrics': metrics, 'order': min(v.id for v, _ in rows)}


def _by_band(segments):
    """For each standard band, the broker size band that fits it best, per metric.
    Preference: exact match, then a narrower slice, then the narrowest wider range."""
    rank = {'exact': 0, 'within': 1, 'spans': 2}
    out = {}
    for band in sizes.STANDARD_BANDS:
        best = {}
        for seg in segments:
            if seg['kind'] != 'size' or band['key'] not in seg['bands']:
                continue
            width = (seg['size_max'] or 5e6) - (seg['size_min'] or 0)
            for metric, v in seg['metrics'].items():
                score = (rank[seg['fit']], width)
                if metric not in best or score < best[metric][0]:
                    best[metric] = (score, {**v, 'broker_band': seg['segment'], 'broker_range': seg['range'],
                                            'fit': seg['fit']})
        if best:
            out[band['key']] = {m: e for m, (_, e) in best.items()}
    return out


def _trend(metrics, comparisons):
    """Calculated direction-of-travel indicators for one market. Every item names its inputs."""
    items, score = [], 0

    def add(key, label, value, unit, good, detail, inputs):
        nonlocal score
        tone = 0 if good is None else (1 if good else -1)
        score += tone
        items.append({'key': key, 'label': label, 'value': value, 'unit': unit, 'tone': tone,
                      'detail': detail, 'inputs': inputs})

    def change(metric, span):
        comp = comparisons.get(metric, {}).get(span)
        cur = metrics.get(metric)
        if not comp or not cur:
            return None
        pv, pd = comp
        return cur, pv, pd

    for span, word in (('year', 'a year earlier'), ('quarter', 'the prior quarter')):
        c = change('vacancy_rate', span)
        if c:
            cur, pv, pd = c
            bps = round((cur['value'] - pv.value) * 100)
            add(f'vacancy_change_{span}', f'Vacancy vs {word}', bps, 'bps', None if bps == 0 else bps < 0,
                f'{cur["value"]:g}% now vs {pv.value:g}% in {pv.quarter} ({pd.source})', [cur['id'], pv.id])
            break
    for span, word in (('year', 'a year earlier'), ('quarter', 'the prior quarter')):
        c = change('asking_rent', span)
        if c and c[1].value:
            cur, pv, pd = c
            now = cur.get('annualized', cur['value'])
            pct = round((now - _annual(pv)) / _annual(pv) * 100, 1)
            add(f'rent_change_{span}', f'Asking rent vs {word}', pct, 'percent', None if pct == 0 else pct > 0,
                f'${cur["value"]:.2f} now vs ${pv.value:.2f} in {pv.quarter} ({pd.source})', [cur['id'], pv.id])
            break
    c = change('under_construction', 'year') or change('under_construction', 'quarter')
    if c and c[1].value:
        cur, pv, pd = c
        pct = round((cur['value'] - pv.value) / pv.value * 100, 1)
        add('pipeline_change', f'Under construction vs {pv.quarter}', pct, 'percent', None if pct == 0 else pct < 0,
            f'{cur["value"]:,.0f} SF now vs {pv.value:,.0f} SF in {pv.quarter} ({pd.source})', [cur['id'], pv.id])

    inv, uc = metrics.get('inventory'), metrics.get('under_construction')
    if inv and uc and inv['value'] and inv['doc']['id'] == uc['doc']['id']:
        pct = round(uc['value'] / inv['value'] * 100, 1)
        add('pipeline_share', 'Pipeline as share of inventory', pct, 'percent', True if pct < 2 else False if pct > 5 else None,
            f'{uc["value"]:,.0f} SF under construction ÷ {inv["value"]:,.0f} SF inventory', [uc['id'], inv['id']])
    ab = metrics.get('net_absorption_qtr')
    if ab:
        add('absorption_sign', 'Net absorption this quarter', ab['value'], 'sf', None if ab['value'] == 0 else ab['value'] > 0,
            f'{ab["value"]:,.0f} SF ({ab["doc"]["source"]})', [ab['id']])
        dl = metrics.get('deliveries_qtr')
        if dl and dl['doc']['id'] == ab['doc']['id'] and dl['value']:
            ahead = ab['value'] > dl['value']
            add('demand_vs_supply', 'Absorption vs deliveries', round(ab['value'] - dl['value']), 'sf', ahead,
                f'{ab["value"]:,.0f} SF absorbed vs {dl["value"]:,.0f} SF delivered in the quarter', [ab['id'], dl['id']])
    rg = metrics.get('rent_growth_yoy')
    if rg and not any(i['key'].startswith('rent_change') for i in items):
        add('rent_growth', 'Rent growth (as published)', rg['value'], 'percent', None if rg['value'] == 0 else rg['value'] > 0,
            f'{rg["value"]:g}% year over year ({rg["doc"]["source"]})', [rg['id']])

    # A direction is only called when earlier-period figures back it: a single quarter's levels
    # (positive absorption, a small pipeline) say where a market is, not where it is heading.
    has_history = any(i['key'].startswith(('vacancy_change', 'rent_change', 'pipeline_change')) for i in items)
    scored = [i for i in items if i['tone']]
    if not has_history or len(scored) < 2:
        direction = 'unknown'
    elif score >= 2:
        direction = 'improving'
    elif score <= -2:
        direction = 'weakening'
    else:
        direction = 'mixed'
    return {'direction': direction, 'score': score, 'signals': items, 'has_history': has_history}


def build_snapshot():
    """Everything the market pages need, grouped by market."""
    by_market = defaultdict(lambda: defaultdict(list))        # market → quarter_idx → [(value, doc)]
    for v, doc in _published():
        if v.quarter_idx is not None:
            by_market[v.market][v.quarter_idx].append((v, doc))

    expected = quarters.latest_published()
    expected_idx = quarters.index(expected)
    regions = {m.name: m.region for m in Market.query.all()}
    markets = []

    for name, by_quarter in by_market.items():
        # A market's "latest quarter" is the newest quarter some report actually covers
        # (not merely a comparison column in a later report).
        own = [q for q, rows in by_quarter.items() if any(not v.period_offset for v, _ in rows)]
        if not own:
            continue
        latest_idx = max(own)

        totals_by_q = {}
        chosen_by_quarter = {}
        for q_idx, rows in by_quarter.items():
            totals = [(v, d) for v, d in rows if not v.segment]
            own_rows = [(v, d) for v, d in rows if not v.period_offset]
            per_doc = defaultdict(int)
            for v, d in ([r for r in own_rows if not r[0].segment] or own_rows or totals or rows):
                per_doc[d.id] += 1
            docs = {d.id: d for _, d in rows}
            primary_id = max(per_doc, key=lambda i: (per_doc[i], _recency(docs[i])))
            by_metric = defaultdict(list)
            for v, d in totals:
                by_metric[v.metric].append((v, d))
            totals_by_q[q_idx] = by_metric
            chosen_by_quarter[q_idx] = {
                'primary': docs[primary_id], 'docs': {d.id: d for _, d in own_rows} or docs, 'rows': rows,
                'metrics': {m: _pick(c, primary_id) for m, c in by_metric.items()},
            }

        latest = chosen_by_quarter[latest_idx]
        metrics, comparisons = {}, defaultdict(dict)
        for key, (v, d) in latest['metrics'].items():
            entry = value_dict(v, d)
            prev = _comparison((v, d), totals_by_q, latest_idx - 1, key)
            if prev:
                comparisons[key]['quarter'] = prev
                entry['previous'] = {'value': prev[0].value, 'quarter': prev[0].quarter, 'doc_id': prev[1].id}
            year = _comparison((v, d), totals_by_q, latest_idx - 4, key)
            if year:
                comparisons[key]['year'] = year
                entry['year_ago'] = {'value': year[0].value, 'quarter': year[0].quarter, 'doc_id': year[1].id}
            metrics[key] = entry

        seg_rows = defaultdict(list)
        for v, d in latest['rows']:
            if v.segment and not v.period_offset:
                seg_rows[v.segment].append((v, d))
        segments = [_segment_entry(label, rows, latest['primary'].id) for label, rows in seg_rows.items()]
        segments.sort(key=lambda s: (s['kind'] != 'size', s['size_min'] if s['size_min'] is not None else 0, s['order']))

        history = []
        for q_idx in sorted(chosen_by_quarter, reverse=True):
            c = chosen_by_quarter[q_idx]
            history.append({
                'quarter': quarters.from_index(q_idx),
                'source': c['primary'].source,
                'doc_id': c['primary'].id,
                'restated': all(v.period_offset for v, _ in c['metrics'].values()) if c['metrics'] else False,
                'metrics': {k: value_dict(v, d) for k, (v, d) in c['metrics'].items()},
            })

        markets.append({
            'market': name,
            'region': regions.get(name) or catalog.region_for(name),
            'quarter': quarters.from_index(latest_idx),
            'quarters_behind': max(0, expected_idx - latest_idx),
            'is_current': latest_idx >= expected_idx,
            'primary_source': latest['primary'].source,
            'sources': sorted({d.source for d in latest['docs'].values() if d.source}),
            'documents': [{'id': d.id, 'source': d.source, 'title': d.title or d.filename, 'origin': d.origin}
                          for d in latest['docs'].values()],
            'metrics': metrics,
            'segments': segments,
            'by_band': _by_band(segments),
            'trend': _trend(metrics, comparisons),
            'history': history,
        })

    markets.sort(key=lambda m: m['market'])
    return {
        'expected_quarter': expected,
        'calendar_quarter': quarters.calendar_quarter(),
        'metrics': [{'key': m['key'], 'label': m['label'], 'unit': m['unit']} for m in catalog.METRICS],
        'headline_metrics': catalog.HEADLINE_METRICS,
        'size_bands': [{k: b[k] for k in ('key', 'label', 'short', 'note')} for b in sizes.STANDARD_BANDS],
        'markets': markets,
    }


# ── Underwriting sheet ───────────────────────────────────────────────────────

_legacy_cache = None
_LEGACY_BAND = {'0-50K SF (Small-Bay)': 'b1', '50K-100K SF': 'b2', '100K-250K SF': 'b3',
                '250K-500K SF (Cross-Dock)': 'b4', '500K-750K SF (Cross-Dock)': 'b5', '750K SF+ (Mega-DC)': 'b6'}


def legacy_estimates():
    """Starter estimates carried over from the first version of the site. They are NOT from
    broker reports and are always labelled as unverified wherever they are shown."""
    global _legacy_cache
    if _legacy_cache is None:
        path = os.path.join(_HERE, 'legacy_estimates.json')
        out = {}
        if os.path.exists(path):
            with open(path, encoding='utf-8') as fh:
                raw = json.load(fh)
            for market, m in raw.get('markets', {}).items():
                out[market] = {_LEGACY_BAND[label]: values for label, values in m.get('by_size', {}).items()
                               if label in _LEGACY_BAND}
        _legacy_cache = out
    return _legacy_cache


def underwriting_markets(snapshot=None):
    snapshot = snapshot or build_snapshot()
    with_data = {m['market']: m for m in snapshot['markets']}
    assumed = defaultdict(int)
    for market, in db.session.query(Assumption.market).all():
        assumed[market] += 1
    names = set(with_data) | set(legacy_estimates()) | set(assumed) | {m.name for m in Market.query.filter_by(tracked=True)}
    regions = {m.name: m.region for m in Market.query.all()}
    out = []
    for name in sorted(names):
        m = with_data.get(name)
        out.append({'market': name, 'region': regions.get(name) or catalog.region_for(name),
                    'quarter': m['quarter'] if m else None, 'quarters_behind': m['quarters_behind'] if m else None,
                    'broker_figures': len(m['metrics']) if m else 0, 'has_size_data': bool(m and m['by_band']),
                    'assumptions': assumed.get(name, 0), 'has_estimates': name in legacy_estimates()})
    return out


def underwriting_sheet(market, snapshot=None):
    """The underwriting sheet for one market: for every standard size band and field, the broker
    figure (if any report gives one), the user's own assumption (if entered) and the unverified
    starter estimate (if one exists). The site decides which to show; they are never merged."""
    snapshot = snapshot or build_snapshot()
    m = next((x for x in snapshot['markets'] if x['market'] == market), None)
    assumptions = defaultdict(dict)
    for a in Assumption.query.filter_by(market=market).all():
        assumptions[a.band][a.field] = {'value': a.value, 'note': a.note or '',
                                        'updated_at': a.updated_at.isoformat() + 'Z' if a.updated_at else None}
    estimates = legacy_estimates().get(market, {})

    bands = []
    for band in sizes.STANDARD_BANDS:
        cells = {}
        for field in catalog.UNDERWRITING_FIELDS:
            broker = None
            metric = field['metric']
            if m and metric:
                v = m['by_band'].get(band['key'], {}).get(metric)
                scope = 'size'
                if not v:
                    v, scope = m['metrics'].get(metric), 'market'
                if v:
                    value = v['value']
                    if field['key'] == 'rent' and v.get('annualized') is not None:
                        value = v['annualized']
                    broker = {**v, 'sheet_value': value, 'scope': scope}
            cells[field['key']] = {
                'broker': broker,
                'assumption': assumptions.get(band['key'], {}).get(field['key']),
                'estimate': estimates.get(band['key'], {}).get(field['key']),
            }
        bands.append({'key': band['key'], 'label': band['label'], 'note': band['note'], 'cells': cells})

    regions = {x.name: x.region for x in Market.query.filter_by(name=market).all()}
    return {
        'market': market, 'region': regions.get(market) or catalog.region_for(market),
        'quarter': m['quarter'] if m else None, 'quarters_behind': m['quarters_behind'] if m else None,
        'sources': m['sources'] if m else [],
        'fields': catalog.UNDERWRITING_FIELDS, 'bands': bands,
        'has_estimates': bool(estimates),
        'segments': m['segments'] if m else [],
    }


# ── Coverage ─────────────────────────────────────────────────────────────────

def coverage():
    """For each tracked market: do we hold a report for the latest published quarter?
    If not, why not (blocked site, dead link, nothing found) and where to get it."""
    expected = quarters.latest_published()
    expected_idx = quarters.index(expected)

    published = defaultdict(lambda: defaultdict(set))     # market → quarter_idx → {source}
    for v, doc in _published():
        if v.quarter_idx is not None and not v.period_offset:
            published[v.market][v.quarter_idx].add(doc.source)

    pending = defaultdict(set)
    rows = (db.session.query(MetricValue.market, MetricValue.document_id)
            .filter(MetricValue.status == 'needs_review', MetricValue.quarter_idx >= expected_idx).distinct().all())
    for market, doc_id in rows:
        pending[market].add(doc_id)

    targets = defaultdict(list)
    for t in ScanTarget.query.order_by(ScanTarget.source).all():
        targets[t.market].append({
            'id': t.id, 'source': t.source, 'url': t.url, 'result': t.last_result, 'detail': t.detail,
            'http_status': t.http_status, 'last_checked': t.last_checked.isoformat() + 'Z' if t.last_checked else None,
        })

    from models import FinderRun
    finder = {}
    for run in FinderRun.query.filter_by(quarter=expected).order_by(FinderRun.started_at).all():
        links = [l for l in json.loads(run.links_json or '[]') if not l.get('document_id')]
        finder[run.market] = {'status': run.status, 'detail': run.detail,
                              'at': (run.finished_at or run.started_at).isoformat() + 'Z',
                              'links': [{'url': l['url'], 'result': l.get('result', '')} for l in links[:3]]}

    names = {m.name: m for m in Market.query.filter_by(tracked=True).all()}
    out = []
    for name in sorted(names):
        held = published.get(name, {})
        latest_idx = max(held) if held else None
        if latest_idx is not None and latest_idx >= expected_idx:
            status = 'current'
        elif pending.get(name):
            status = 'review'
        else:
            status = 'missing'
        out.append({
            'market': name, 'region': names[name].region, 'status': status,
            'latest_quarter': quarters.from_index(latest_idx) if latest_idx is not None else None,
            'sources': sorted(held.get(latest_idx, [])) if latest_idx is not None else [],
            'review_documents': sorted(pending.get(name, [])),
            'targets': targets.get(name, []),
            'search': finder.get(name),
        })
    counts = defaultdict(int)
    for row in out:
        counts[row['status']] += 1
    return {'expected_quarter': expected, 'markets': out,
            'counts': {'current': counts['current'], 'review': counts['review'], 'missing': counts['missing'],
                       'total': len(out)}}


def document_dict(doc, counts=None):
    c = counts or {}
    return {
        'id': doc.id, 'filename': doc.filename, 'origin': doc.origin, 'mime': doc.mime, 'size': doc.size,
        'page_count': doc.page_count, 'has_text': doc.has_text, 'report_url': doc.report_url,
        'source': doc.source, 'title': doc.title, 'quarter': doc.quarter, 'scope': doc.scope,
        'primary_market': doc.primary_market, 'status': doc.status, 'error': doc.error,
        'created_at': doc.created_at.isoformat() + 'Z' if doc.created_at else None,
        'processed_at': doc.processed_at.isoformat() + 'Z' if doc.processed_at else None,
        'value_counts': {'published': c.get('published', 0), 'needs_review': c.get('needs_review', 0),
                         'rejected': c.get('rejected', 0)},
        'markets': c.get('markets', []),
    }


def document_counts(doc_ids=None):
    q = db.session.query(MetricValue.document_id, MetricValue.status, MetricValue.market)
    if doc_ids is not None:
        q = q.filter(MetricValue.document_id.in_(doc_ids))
    out = defaultdict(lambda: {'published': 0, 'needs_review': 0, 'rejected': 0, 'markets': set()})
    for doc_id, status, market in q.all():
        bucket = 'published' if status in PUBLISHED else status
        if bucket in out[doc_id]:
            out[doc_id][bucket] += 1
        out[doc_id]['markets'].add(market)
    for c in out.values():
        c['markets'] = sorted(c['markets'])
    return out
