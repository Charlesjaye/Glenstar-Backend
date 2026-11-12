"""Read-side queries: what the site displays. Only published values (verified
automatically, or confirmed/edited/entered by a person) are ever returned here."""
from collections import defaultdict

import catalog
import quarters
from models import PUBLISHED, Document, Market, MetricValue, ScanTarget, db


def value_dict(v, doc):
    out = {
        'id': v.id, 'metric': v.metric, 'segment': v.segment or '', 'value': v.value, 'unit': v.unit,
        'period': v.period, 'printed': v.printed, 'quote': v.quote, 'page': v.page, 'note': v.note or '',
        'status': v.status, 'quarter': v.quarter, 'market': v.market,
        'doc': {'id': doc.id, 'source': doc.source, 'title': doc.title or doc.filename, 'origin': doc.origin},
    }
    if v.metric == 'asking_rent' and v.period == 'mo':
        out['annualized'] = round(v.value * 12, 4)
    return out


def _published():
    """All published values with their documents, newest quarter first."""
    return (db.session.query(MetricValue, Document)
            .join(Document, MetricValue.document_id == Document.id)
            .filter(MetricValue.status.in_(PUBLISHED), Document.status.in_(('ready', 'needs_review')))
            .all())


def _pick(candidates, primary_doc_id):
    """Choose one value for a market/quarter/metric: the primary report's if it has one,
    otherwise the most recently read report's. Values are never averaged or blended."""
    for v, doc in candidates:
        if doc.id == primary_doc_id:
            return v, doc
    return max(candidates, key=lambda c: (c[1].processed_at or c[1].created_at, c[0].id))


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
        chosen_by_quarter = {}
        for q_idx, rows in by_quarter.items():
            totals = [(v, d) for v, d in rows if not v.segment]
            per_doc = defaultdict(int)
            for v, d in totals or rows:
                per_doc[d.id] += 1
            docs = {d.id: d for _, d in rows}
            primary_id = max(per_doc, key=lambda i: (per_doc[i], docs[i].processed_at or docs[i].created_at))
            by_metric = defaultdict(list)
            for v, d in totals:
                by_metric[v.metric].append((v, d))
            chosen_by_quarter[q_idx] = {
                'primary': docs[primary_id], 'docs': docs, 'rows': rows,
                'metrics': {m: _pick(c, primary_id) for m, c in by_metric.items()},
            }

        latest_idx = max(chosen_by_quarter)
        latest = chosen_by_quarter[latest_idx]
        previous = chosen_by_quarter.get(latest_idx - 1)

        metrics = {}
        for key, (v, d) in latest['metrics'].items():
            entry = value_dict(v, d)
            # Quarter-over-quarter change, only against the same broker's previous report
            # (different brokers define markets differently, so cross-broker changes mislead).
            if previous and key in previous['metrics']:
                pv, pd = previous['metrics'][key]
                if pd.source == d.source and pv.unit == v.unit and pv.period == v.period:
                    entry['previous'] = {'value': pv.value, 'quarter': pv.quarter, 'doc_id': pd.id}
            metrics[key] = entry

        segments = defaultdict(dict)
        for v, d in latest['rows']:
            if v.segment and (v.metric not in segments[v.segment] or d.id == latest['primary'].id):
                segments[v.segment][v.metric] = value_dict(v, d)

        history = []
        for q_idx in sorted(chosen_by_quarter, reverse=True):
            c = chosen_by_quarter[q_idx]
            history.append({
                'quarter': quarters.from_index(q_idx),
                'source': c['primary'].source,
                'doc_id': c['primary'].id,
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
            'segments': [{'segment': s, 'metrics': m} for s, m in sorted(segments.items())],
            'history': history,
        })

    markets.sort(key=lambda m: m['market'])
    return {
        'expected_quarter': expected,
        'calendar_quarter': quarters.calendar_quarter(),
        'metrics': [{'key': m['key'], 'label': m['label'], 'unit': m['unit']} for m in catalog.METRICS],
        'headline_metrics': catalog.HEADLINE_METRICS,
        'markets': markets,
    }


def coverage():
    """For each tracked market: do we hold a report for the latest published quarter?
    If not, why not (blocked site, dead link, nothing found) and where to get it."""
    expected = quarters.latest_published()
    expected_idx = quarters.index(expected)

    published = defaultdict(lambda: defaultdict(set))     # market → quarter_idx → {source}
    for v, doc in _published():
        if v.quarter_idx is not None:
            published[v.market][v.quarter_idx].add(doc.source)

    pending = defaultdict(set)                            # market → {doc_id} waiting for review, current quarter
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
