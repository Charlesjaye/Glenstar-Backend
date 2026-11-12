"""Static reference data: tracked markets, brokerages, metric definitions and
the broker pages the scanner checks. No market statistics live in this file —
every number shown on the site comes from an ingested broker document."""
import json
import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))

with open(os.path.join(_HERE, 'catalog.json'), encoding='utf-8') as fh:
    _DATA = json.load(fh)

MARKETS = _DATA['markets']           # [{name, region, aliases}]
SOURCE_PAGES = _DATA['sources']      # [{source, market, url}]

BROKERAGES = [
    'JLL', 'CBRE', 'Cushman & Wakefield', 'Colliers', 'Newmark', 'Avison Young',
    'Savills', 'Lee & Associates', 'Transwestern', 'Kidder Mathews', 'NAI',
    'Marcus & Millichap', 'CoStar', 'Other',
]

# Metric definitions. `unit` is the canonical stored unit:
#   percent        value as printed, e.g. 7.2 for 7.2%
#   sf             square feet (a printed "1.2 MSF" is stored as 1200000)
#   usd_psf        dollars per square foot (rent values also carry a period: yr / mo)
#   months         a duration in months
# `range` is a plausibility window; values outside it are held for review.
METRICS = [
    {'key': 'vacancy_rate',         'label': 'Vacancy rate',              'unit': 'percent', 'range': (0, 40),
     'definition': 'Total (or overall) vacancy rate for the industrial market, as a percent.'},
    {'key': 'availability_rate',    'label': 'Availability rate',         'unit': 'percent', 'range': (0, 50),
     'definition': 'Availability rate (vacant plus marketed space), as a percent. Not the vacancy rate.'},
    {'key': 'net_absorption_qtr',   'label': 'Net absorption (quarter)',  'unit': 'sf', 'range': (-60e6, 60e6),
     'definition': 'Net absorption for the report quarter only, in square feet.'},
    {'key': 'net_absorption_ytd',   'label': 'Net absorption (YTD)',      'unit': 'sf', 'range': (-120e6, 120e6),
     'definition': 'Year-to-date net absorption, in square feet.'},
    {'key': 'asking_rent',          'label': 'Asking rent',               'unit': 'usd_psf', 'range': (0.2, 60),
     'definition': 'Average asking rent per square foot (state whether per year or per month; note NNN/gross if stated).'},
    {'key': 'rent_growth_yoy',      'label': 'Rent growth (YoY)',         'unit': 'percent', 'range': (-40, 60),
     'definition': 'Year-over-year change in asking rent, as a percent, only if the report states it.'},
    {'key': 'under_construction',   'label': 'Under construction',        'unit': 'sf', 'range': (0, 100e6),
     'definition': 'Space under construction at quarter end, in square feet.'},
    {'key': 'deliveries_qtr',       'label': 'Deliveries (quarter)',      'unit': 'sf', 'range': (0, 60e6),
     'definition': 'New construction completions/deliveries during the report quarter, in square feet.'},
    {'key': 'deliveries_ytd',       'label': 'Deliveries (YTD)',          'unit': 'sf', 'range': (0, 120e6),
     'definition': 'Year-to-date completions/deliveries, in square feet.'},
    {'key': 'leasing_activity_qtr', 'label': 'Leasing activity (quarter)', 'unit': 'sf', 'range': (0, 80e6),
     'definition': 'Gross leasing activity during the report quarter, in square feet.'},
    {'key': 'leasing_activity_ytd', 'label': 'Leasing activity (YTD)',    'unit': 'sf', 'range': (0, 200e6),
     'definition': 'Year-to-date gross leasing activity, in square feet.'},
    {'key': 'inventory',            'label': 'Inventory',                 'unit': 'sf', 'range': (1e6, 3e9),
     'definition': 'Total industrial inventory tracked in the market, in square feet.'},
    {'key': 'cap_rate',             'label': 'Cap rate',                  'unit': 'percent', 'range': (2, 15),
     'definition': 'Average industrial cap rate, as a percent, only if the report states it.'},
    {'key': 'sale_price_psf',       'label': 'Sale price',                'unit': 'usd_psf', 'range': (10, 1500),
     'definition': 'Average industrial sale price per square foot, only if the report states it.'},
    {'key': 'construction_cost_psf', 'label': 'Construction cost',        'unit': 'usd_psf', 'range': (20, 600),
     'definition': 'Industrial construction cost per square foot, only if the report states it.'},
    # Underwriting inputs that some broker reports publish
    {'key': 'free_rent_months',     'label': 'Free rent',                 'unit': 'months', 'range': (0, 24),
     'definition': 'Typical free rent / rent abatement on new leases, in months, only if the report states it.'},
    {'key': 'ti_allowance_psf',     'label': 'TI allowance',              'unit': 'usd_psf', 'range': (0, 150),
     'definition': 'Typical tenant improvement allowance per square foot on new leases, only if the report states it.'},
    {'key': 'lease_term_months',    'label': 'Lease term',                'unit': 'months', 'range': (6, 360),
     'definition': 'Average lease term, in months (a term printed in years is converted: 5 years = 60), only if stated.'},
    {'key': 'rent_escalation_pct',  'label': 'Rent escalations',          'unit': 'percent', 'range': (0, 10),
     'definition': 'Typical annual contractual rent escalation, as a percent, only if the report states it.'},
    {'key': 'opex_psf',             'label': 'Operating expenses',        'unit': 'usd_psf', 'range': (0, 20),
     'definition': 'Typical operating expenses / NNN charges per square foot per year, only if the report states it.'},
    {'key': 'taxes_psf',            'label': 'Real estate taxes',         'unit': 'usd_psf', 'range': (0, 15),
     'definition': 'Typical real estate taxes per square foot per year, only if the report states it.'},
]
METRIC_BY_KEY = {m['key']: m for m in METRICS}
METRIC_KEYS = [m['key'] for m in METRICS]

# Metrics shown as columns on the market table, in order.
HEADLINE_METRICS = ['vacancy_rate', 'asking_rent', 'net_absorption_qtr', 'net_absorption_ytd',
                    'under_construction', 'rent_growth_yoy']


def _norm(name):
    s = (name or '').lower()
    s = s.replace('&', ' and ').replace('saint ', 'st ').replace('ft.', 'fort').replace('ft ', 'fort ')
    s = re.sub(r'\b(industrial|market|metro|metropolitan|area|greater|msa|report|marketbeat|figures)\b', ' ', s)
    s = re.sub(r',\s*[a-z]{2}\b', ' ', s)          # ", TX"
    s = re.sub(r'[^a-z0-9]+', ' ', s)
    return ' '.join(s.split())


_LOOKUP = {}
for _m in MARKETS:
    _LOOKUP[_norm(_m['name'])] = _m['name']
    for _a in _m.get('aliases', []):
        _LOOKUP.setdefault(_norm(_a), _m['name'])

_REGION = {m['name']: m['region'] for m in MARKETS}


def canonical_market(printed, extra_lookup=None):
    """Map a market name as printed in a report to a tracked market name.
    Returns (canonical_name, matched) — unmatched names are returned cleaned up,
    never guessed onto a different market."""
    cleaned = ' '.join((printed or '').split())
    key = _norm(cleaned)
    if not key:
        return None, False
    if extra_lookup and key in extra_lookup:
        return extra_lookup[key], True
    if key in _LOOKUP:
        return _LOOKUP[key], True
    return cleaned, False


def region_for(market):
    return _REGION.get(market, '')


def normalize_brokerage(name):
    s = (name or '').lower()
    table = [
        ('jll', 'JLL'), ('jones lang', 'JLL'), ('cbre', 'CBRE'), ('cushman', 'Cushman & Wakefield'),
        ('c&w', 'Cushman & Wakefield'), ('colliers', 'Colliers'), ('newmark', 'Newmark'),
        ('avison', 'Avison Young'), ('savills', 'Savills'), ('lee & associates', 'Lee & Associates'),
        ('lee and associates', 'Lee & Associates'), ('transwestern', 'Transwestern'),
        ('kidder', 'Kidder Mathews'), ('nai', 'NAI'), ('marcus', 'Marcus & Millichap'), ('costar', 'CoStar'),
    ]
    for needle, canon in table:
        if needle in s:
            return canon
    return (name or '').strip() or 'Other'


# Metrics for which a report's prior-period comparison figures are also kept, to show direction.
TREND_METRICS = ['vacancy_rate', 'availability_rate', 'asking_rent', 'net_absorption_qtr', 'under_construction',
                 'deliveries_qtr', 'inventory']

# The underwriting sheet. `metric` names the broker metric that can fill the field; fields
# without one are never published by brokers and can only be a Glenstar assumption.
UNDERWRITING_FIELDS = [
    {'key': 'rent',         'label': 'Asking rent',         'unit': 'usd_psf_yr', 'group': 'Revenue',      'metric': 'asking_rent'},
    {'key': 'rent_growth',  'label': 'Rent growth',         'unit': 'percent',    'group': 'Revenue',      'metric': 'rent_growth_yoy'},
    {'key': 'vacancy',      'label': 'Vacancy',             'unit': 'percent',    'group': 'Revenue',      'metric': 'vacancy_rate'},
    {'key': 'escalation',   'label': 'Rent escalations',    'unit': 'percent',    'group': 'Revenue',      'metric': 'rent_escalation_pct'},
    {'key': 'cost',         'label': 'Construction cost',   'unit': 'usd_psf',    'group': 'Construction', 'metric': 'construction_cost_psf'},
    {'key': 'free_rent',    'label': 'Free rent',           'unit': 'months',     'group': 'Leasing',      'metric': 'free_rent_months'},
    {'key': 'ti_new',       'label': 'TI — new lease',      'unit': 'usd_psf',    'group': 'Leasing',      'metric': 'ti_allowance_psf'},
    {'key': 'ti_renewal',   'label': 'TI — renewal',        'unit': 'usd_psf',    'group': 'Leasing',      'metric': None},
    {'key': 'lc',           'label': 'Leasing commission',  'unit': 'percent',    'group': 'Leasing',      'metric': None},
    {'key': 'lease_term',   'label': 'Lease term',          'unit': 'months',     'group': 'Leasing',      'metric': 'lease_term_months'},
    {'key': 'renewal_prob', 'label': 'Renewal probability', 'unit': 'percent',    'group': 'Leasing',      'metric': None},
    {'key': 'downtime',     'label': 'Downtime on rollover', 'unit': 'months',    'group': 'Leasing',      'metric': None},
    {'key': 'cap',          'label': 'Exit cap rate',       'unit': 'percent',    'group': 'Exit',         'metric': 'cap_rate'},
    {'key': 'opex',         'label': 'Operating expenses',  'unit': 'usd_psf',    'group': 'Operating',    'metric': 'opex_psf'},
    {'key': 'taxes',        'label': 'Real estate taxes',   'unit': 'usd_psf',    'group': 'Operating',    'metric': 'taxes_psf'},
    {'key': 'cap_reserve',  'label': 'Capital reserve',     'unit': 'usd_psf',    'group': 'Operating',    'metric': None},
]
UNDERWRITING_FIELD_BY_KEY = {f['key']: f for f in UNDERWRITING_FIELDS}
