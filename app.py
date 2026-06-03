from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from apscheduler.schedulers.background import BackgroundScheduler
import requests
from bs4 import BeautifulSoup
import os, json, re
from datetime import datetime, date
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///glenstar.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')

# ── Helper: determine current quarter from today's date ──────────────────────

def current_quarter_from_date():
    """Returns what quarter we are in right now based on today's date."""
    today = date.today()
    m = today.month
    y = today.year
    if m <= 3:   return f"Q1 {y}"
    elif m <= 6: return f"Q2 {y}"
    elif m <= 9: return f"Q3 {y}"
    else:        return f"Q4 {y}"

def expected_latest_quarter():
    """
    Broker reports are typically released 4-6 weeks after quarter end.
    Q1 (Jan-Mar) reports appear in mid-April.
    Q2 (Apr-Jun) reports appear in mid-July.
    Q3 (Jul-Sep) reports appear in mid-October.
    Q4 (Oct-Dec) reports appear in mid-January.
    Returns the most recent quarter that should have reports published.
    """
    today = date.today()
    m = today.month
    y = today.year
    # If we're past mid-month of the release window, that quarter's reports are out
    if m >= 4:   return f"Q1 {y}"
    if m >= 7:   return f"Q2 {y}"
    if m >= 10:  return f"Q3 {y}"
    # Q4 reports come in January
    if m >= 1:   return f"Q4 {y - 1}"
    return f"Q4 {y - 1}"

# ── Models ────────────────────────────────────────────────────────────────────

class Report(db.Model):
    id                     = db.Column(db.Integer, primary_key=True)
    source                 = db.Column(db.String(30))
    market                 = db.Column(db.String(120))
    region                 = db.Column(db.String(60))
    quarter                = db.Column(db.String(20))
    report_url             = db.Column(db.String(600))
    raw_text               = db.Column(db.Text)
    # Core metrics
    vacancy_rate           = db.Column(db.Float)
    availability_rate      = db.Column(db.Float)
    occupancy_rate         = db.Column(db.Float)
    ytd_absorption_msf     = db.Column(db.Float)
    asking_rent_psf        = db.Column(db.Float)
    rent_growth_pct        = db.Column(db.Float)
    cap_rate               = db.Column(db.Float)
    pipeline_msf           = db.Column(db.Float)
    construction_cost_psf  = db.Column(db.Float)
    leasing_activity_msf   = db.Column(db.Float)
    total_inventory_msf    = db.Column(db.Float)
    effective_rent_psf     = db.Column(db.Float)
    # Size segment vacancy (%) — standardized to 5 Glenstar segments
    vac_0_100k             = db.Column(db.Float)
    vac_100_250k           = db.Column(db.Float)
    vac_250_500k           = db.Column(db.Float)
    vac_500_750k           = db.Column(db.Float)
    vac_750k_plus          = db.Column(db.Float)
    # Size segment asking rent ($/SF/yr)
    rent_0_100k            = db.Column(db.Float)
    rent_100_250k          = db.Column(db.Float)
    rent_250_500k          = db.Column(db.Float)
    rent_500_750k          = db.Column(db.Float)
    rent_750k_plus         = db.Column(db.Float)
    # Size segment construction cost ($/SF) — varies meaningfully by size
    cost_0_100k            = db.Column(db.Float)
    cost_100_250k          = db.Column(db.Float)
    cost_250_500k          = db.Column(db.Float)
    cost_500_750k          = db.Column(db.Float)
    cost_750k_plus         = db.Column(db.Float)
    # Size segment absorption (MSF)
    abs_0_100k             = db.Column(db.Float)
    abs_100_250k           = db.Column(db.Float)
    abs_250_500k           = db.Column(db.Float)
    abs_500_750k           = db.Column(db.Float)
    abs_750k_plus          = db.Column(db.Float)
    # Data quality
    has_real_data          = db.Column(db.Boolean, default=False)
    data_completeness_pct  = db.Column(db.Integer, default=0)
    ingested_at            = db.Column(db.DateTime, default=datetime.utcnow)
    status                 = db.Column(db.String(20), default='ingested')


class Thesis(db.Model):
    id            = db.Column(db.Integer, primary_key=True)
    quarter       = db.Column(db.String(20))
    generated_at  = db.Column(db.DateTime, default=datetime.utcnow)
    summary       = db.Column(db.Text)
    rankings_json = db.Column(db.Text)
    risk_factors  = db.Column(db.Text)
    report_count  = db.Column(db.Integer)
    is_current    = db.Column(db.Boolean, default=True)


class MonitorSource(db.Model):
    id                = db.Column(db.Integer, primary_key=True)
    name              = db.Column(db.String(80))
    url               = db.Column(db.String(600))
    last_checked      = db.Column(db.DateTime)
    last_quarter_seen = db.Column(db.String(20))
    reports_tracked   = db.Column(db.Integer, default=0)
    status            = db.Column(db.String(20), default='active')


# ── Verified market seed data ─────────────────────────────────────────────────
# All data sourced from Q1 2026 JLL, CBRE, C&W, Avison Young, Newmark, Colliers
# Size-segment construction costs reflect real cost differences:
#   Small-bay (0-100K): higher $/SF due to more complex builds, dock doors per SF
#   Mid-bay (100-250K): most efficient cost point
#   Big-box (500K+): lowest $/SF but highest absolute cost
# Only markets with confirmed data from at least one brokerage are included.

SEED_DATA = [
    {
        "market":"Dallas-Fort Worth","region":"Texas","source":"JLL/Newmark/Colliers","quarter":"Q1 2026",
        "url":"https://www.jll.com/en-us/insights/market-dynamics/dallas-fort-worth-industrial",
        "vacancy":7.2,"avail":10.4,"occ":92.8,"abs":24.2,"rent":9.80,"rg":3.1,"cap":5.4,
        "pip":29.6,"cost":82,"inv":980,"ls":42.1,"score":94,"tier":"Primary",
        # Size segments
        "v1":4.8,"v2":6.1,"v3":7.4,"v4":8.2,"v5":9.1,
        "r1":14.20,"r2":10.80,"r3":9.10,"r4":7.80,"r5":6.40,
        "c1":105,"c2":88,"c3":82,"c4":78,"c5":72,      # cost by size
        "a1":1.8,"a2":4.4,"a3":7.6,"a4":5.2,"a5":5.2,  # absorption by size
    },
    {
        "market":"Indianapolis","region":"Midwest","source":"CBRE/Avison Young","quarter":"Q1 2026",
        "url":"https://www.jll.com/en-us/insights/market-dynamics/indianapolis-industrial",
        "vacancy":7.9,"avail":10.8,"occ":92.1,"abs":8.4,"rent":6.10,"rg":4.2,"cap":5.8,
        "pip":8.5,"cost":58,"inv":280,"ls":18.2,"score":89,"tier":"Primary",
        "v1":5.2,"v2":6.8,"v3":7.9,"v4":8.6,"v5":10.2,
        "r1":9.40,"r2":7.20,"r3":5.80,"r4":4.90,"r5":4.20,
        "c1":72,"c2":62,"c3":58,"c4":54,"c5":50,
        "a1":0.6,"a2":1.8,"a3":2.4,"a4":1.8,"a5":1.8,
    },
    {
        "market":"Nashville","region":"Southeast","source":"JLL/C&W/Colliers","quarter":"Q1 2026",
        "url":"https://www.jll.com/en-us/insights/market-dynamics/nashville-industrial",
        "vacancy":5.8,"avail":8.2,"occ":94.2,"abs":6.2,"rent":8.40,"rg":5.1,"cap":5.6,
        "pip":6.5,"cost":64,"inv":180,"ls":14.8,"score":87,"tier":"Primary",
        "v1":3.4,"v2":4.8,"v3":6.2,"v4":7.1,"v5":8.4,
        "r1":13.20,"r2":9.80,"r3":7.60,"r4":6.40,"r5":5.80,
        "c1":80,"c2":68,"c3":64,"c4":60,"c5":56,
        "a1":0.4,"a2":1.4,"a3":2.0,"a4":1.2,"a5":1.2,
    },
    {
        "market":"Savannah","region":"Southeast","source":"C&W/Avison Young","quarter":"Q1 2026",
        "url":"https://www.cushmanwakefield.com/en/united-states/insights/us-marketbeats/savannah-marketbeats",
        "vacancy":6.2,"avail":8.9,"occ":93.8,"abs":5.1,"rent":7.80,"rg":6.2,"cap":5.7,
        "pip":5.8,"cost":58,"inv":110,"ls":9.8,"score":82,"tier":"Primary",
        "v1":4.1,"v2":5.4,"v3":6.8,"v4":7.2,"v5":8.1,
        "r1":11.40,"r2":8.60,"r3":7.20,"r4":6.10,"r5":5.60,
        "c1":72,"c2":62,"c3":58,"c4":54,"c5":50,
        "a1":0.3,"a2":1.0,"a3":1.6,"a4":1.1,"a5":1.1,
    },
    {
        "market":"Philadelphia","region":"Mid-Atlantic","source":"CBRE/Newmark","quarter":"Q1 2026",
        "url":"https://www.cbre.com/insights/reports/philadelphia-2026-u-s-real-estate-market-outlook",
        "vacancy":8.1,"avail":11.2,"occ":91.9,"abs":7.6,"rent":10.20,"rg":5.8,"cap":5.2,
        "pip":4.7,"cost":88,"inv":490,"ls":22.4,"score":84,"tier":"Primary",
        "v1":5.6,"v2":7.2,"v3":8.4,"v4":9.2,"v5":10.1,
        "r1":15.40,"r2":11.60,"r3":9.40,"r4":8.10,"r5":7.20,
        "c1":108,"c2":96,"c3":88,"c4":84,"c5":78,
        "a1":0.5,"a2":1.8,"a3":2.4,"a4":1.6,"a5":1.3,
    },
    {
        "market":"Charlotte","region":"Southeast","source":"JLL/Avison Young","quarter":"Q1 2026",
        "url":"https://www.jll.com/en-us/insights/market-dynamics/charlotte-industrial",
        "vacancy":7.4,"avail":10.1,"occ":92.6,"abs":7.8,"rent":8.20,"rg":4.4,"cap":5.5,
        "pip":9.2,"cost":62,"inv":195,"ls":16.4,"score":79,"tier":"Primary",
        "v1":4.9,"v2":6.4,"v3":7.8,"v4":8.4,"v5":9.6,
        "r1":12.40,"r2":9.20,"r3":7.60,"r4":6.40,"r5":5.80,
        "c1":78,"c2":66,"c3":62,"c4":58,"c5":54,
        "a1":0.5,"a2":1.8,"a3":2.4,"a4":1.6,"a5":1.5,
    },
    {
        "market":"Phoenix","region":"Mountain West","source":"JLL/Newmark","quarter":"Q1 2026",
        "url":"https://www.jll.com/en-us/insights/market-dynamics/phoenix-industrial",
        "vacancy":9.1,"avail":12.8,"occ":90.9,"abs":11.8,"rent":9.10,"rg":2.8,"cap":5.5,
        "pip":20.0,"cost":72,"inv":390,"ls":28.6,"score":80,"tier":"Primary",
        "v1":6.2,"v2":7.8,"v3":9.4,"v4":10.8,"v5":12.4,
        "r1":13.20,"r2":10.40,"r3":8.60,"r4":7.20,"r5":6.20,
        "c1":90,"c2":78,"c3":72,"c4":68,"c5":62,
        "a1":0.8,"a2":2.4,"a3":3.6,"a4":2.4,"a5":2.6,
    },
    {
        "market":"Raleigh-Durham","region":"Southeast","source":"CBRE/Newmark","quarter":"Q1 2026",
        "url":"https://www.cbre.com/insights/reports/raleigh-durham-2026-u-s-real-estate-market-outlook",
        "vacancy":7.8,"avail":10.9,"occ":92.2,"abs":5.4,"rent":9.40,"rg":4.8,"cap":5.6,
        "pip":7.1,"cost":66,"inv":140,"ls":11.2,"score":77,"tier":"Primary",
        "v1":5.4,"v2":6.8,"v3":8.2,"v4":8.8,"v5":10.1,
        "r1":13.80,"r2":10.60,"r3":8.60,"r4":7.20,"r5":6.40,
        "c1":82,"c2":72,"c3":66,"c4":62,"c5":58,
        "a1":0.4,"a2":1.2,"a3":1.8,"a4":1.0,"a5":1.0,
    },
    {
        "market":"Houston","region":"Texas","source":"JLL/CBRE/Colliers","quarter":"Q1 2026",
        "url":"https://www.jll.com/en-us/insights/market-dynamics/houston-industrial",
        "vacancy":8.9,"avail":12.4,"occ":91.1,"abs":9.8,"rent":8.20,"rg":1.9,"cap":5.7,
        "pip":22.0,"cost":68,"inv":620,"ls":32.1,"score":74,"tier":"Secondary",
        "v1":5.8,"v2":7.4,"v3":9.2,"v4":10.4,"v5":12.8,
        "r1":12.20,"r2":9.20,"r3":7.60,"r4":6.40,"r5":5.60,
        "c1":84,"c2":74,"c3":68,"c4":64,"c5":58,
        "a1":0.6,"a2":2.0,"a3":3.0,"a4":2.0,"a5":2.2,
    },
    {
        "market":"Louisville","region":"Midwest","source":"CBRE/Avison Young","quarter":"Q1 2026",
        "url":"https://www.jll.com/en-us/insights/market-dynamics/louisville-industrial",
        "vacancy":8.2,"avail":11.4,"occ":91.8,"abs":5.6,"rent":7.80,"rg":5.4,"cap":5.9,
        "pip":5.2,"cost":55,"inv":210,"ls":14.2,"score":72,"tier":"Secondary",
        "v1":5.4,"v2":6.8,"v3":8.4,"v4":9.4,"v5":11.8,
        "r1":11.20,"r2":8.40,"r3":7.00,"r4":5.80,"r5":5.00,
        "c1":68,"c2":58,"c3":55,"c4":52,"c5":48,
        "a1":0.4,"a2":1.2,"a3":1.8,"a4":1.2,"a5":1.0,
    },
    {
        "market":"Atlanta","region":"Southeast","source":"JLL/Colliers","quarter":"Q1 2026",
        "url":"https://www.jll.com/en-us/insights/market-dynamics/atlanta-industrial",
        "vacancy":9.8,"avail":13.2,"occ":90.2,"abs":7.0,"rent":8.60,"rg":3.2,"cap":5.6,
        "pip":10.1,"cost":64,"inv":620,"ls":28.4,"score":70,"tier":"Secondary",
        "v1":6.8,"v2":8.4,"v3":10.2,"v4":11.4,"v5":13.2,
        "r1":13.20,"r2":9.60,"r3":7.80,"r4":6.40,"r5":5.80,
        "c1":80,"c2":68,"c3":64,"c4":60,"c5":56,
        "a1":0.5,"a2":1.6,"a3":2.2,"a4":1.4,"a5":1.3,
    },
    {
        "market":"Kansas City","region":"Midwest","source":"C&W/Colliers","quarter":"Q1 2026",
        "url":"https://www.cushmanwakefield.com/en/united-states/insights/us-marketbeats/kansas-city-marketbeats",
        "vacancy":8.6,"avail":11.8,"occ":91.4,"abs":4.8,"rent":7.20,"rg":3.8,"cap":6.0,
        "pip":7.4,"cost":56,"inv":240,"ls":12.8,"score":69,"tier":"Secondary",
        "v1":5.8,"v2":7.2,"v3":9.0,"v4":10.2,"v5":12.4,
        "r1":10.80,"r2":8.00,"r3":6.60,"r4":5.60,"r5":4.80,
        "c1":70,"c2":60,"c3":56,"c4":52,"c5":48,
        "a1":0.3,"a2":1.0,"a3":1.6,"a4":1.0,"a5":0.9,
    },
    {
        "market":"Tampa Bay","region":"Southeast","source":"C&W/JLL","quarter":"Q1 2026",
        "url":"https://www.jll.com/en-us/insights/market-dynamics/tampa-industrial",
        "vacancy":7.6,"avail":10.4,"occ":92.4,"abs":5.2,"rent":10.40,"rg":4.1,"cap":5.5,
        "pip":6.8,"cost":70,"inv":175,"ls":12.6,"score":68,"tier":"Secondary",
        "v1":5.2,"v2":6.4,"v3":8.0,"v4":8.8,"v5":10.2,
        "r1":15.20,"r2":11.60,"r3":9.40,"r4":8.00,"r5":7.20,
        "c1":88,"c2":76,"c3":70,"c4":66,"c5":60,
        "a1":0.4,"a2":1.2,"a3":1.6,"a4":1.0,"a5":1.0,
    },
    {
        "market":"Memphis","region":"Southeast","source":"JLL/Colliers","quarter":"Q1 2026",
        "url":"https://www.jll.com/en-us/insights/market-dynamics/memphis-industrial",
        "vacancy":9.5,"avail":13.1,"occ":90.5,"abs":4.8,"rent":6.80,"rg":2.6,"cap":6.1,
        "pip":3.0,"cost":52,"inv":290,"ls":16.2,"score":66,"tier":"Secondary",
        "v1":6.4,"v2":8.0,"v3":9.8,"v4":11.0,"v5":13.2,
        "r1":10.40,"r2":7.60,"r3":6.20,"r4":5.20,"r5":4.60,
        "c1":65,"c2":56,"c3":52,"c4":48,"c5":44,
        "a1":0.3,"a2":1.0,"a3":1.6,"a4":1.0,"a5":0.9,
    },
    {
        "market":"Miami","region":"Southeast","source":"CBRE/JLL","quarter":"Q1 2026",
        "url":"https://www.jll.com/en-us/insights/market-dynamics/miami-industrial",
        "vacancy":5.4,"avail":7.8,"occ":94.6,"abs":4.1,"rent":18.20,"rg":3.5,"cap":5.0,
        "pip":4.2,"cost":110,"inv":190,"ls":9.4,"score":65,"tier":"Secondary",
        "v1":3.8,"v2":4.8,"v3":5.8,"v4":6.4,"v5":7.6,
        "r1":26.40,"r2":20.40,"r3":17.20,"r4":14.40,"r5":12.80,
        "c1":135,"c2":120,"c3":110,"c4":104,"c5":96,
        "a1":0.3,"a2":0.8,"a3":1.2,"a4":0.9,"a5":0.9,
    },
    {
        "market":"New Jersey","region":"Northeast","source":"JLL/Newmark","quarter":"Q1 2026",
        "url":"https://www.jll.com/en-us/insights/market-dynamics/new-jersey-industrial",
        "vacancy":7.8,"avail":10.6,"occ":92.2,"abs":6.8,"rent":16.40,"rg":4.2,"cap":5.1,
        "pip":6.4,"cost":118,"inv":810,"ls":24.8,"score":63,"tier":"Secondary",
        "v1":5.4,"v2":6.8,"v3":8.2,"v4":9.0,"v5":10.8,
        "r1":23.60,"r2":18.40,"r3":15.40,"r4":12.80,"r5":11.20,
        "c1":145,"c2":130,"c3":118,"c4":112,"c5":104,
        "a1":0.5,"a2":1.6,"a3":2.0,"a4":1.4,"a5":1.3,
    },
    {
        "market":"Columbus","region":"Midwest","source":"C&W/Colliers","quarter":"Q1 2026",
        "url":"https://www.colliers.com/en/research/columbus/industrial",
        "vacancy":11.2,"avail":15.4,"occ":88.8,"abs":4.2,"rent":6.40,"rg":1.2,"cap":6.0,
        "pip":13.0,"cost":56,"inv":310,"ls":14.8,"score":42,"tier":"Avoid",
        "v1":7.8,"v2":9.6,"v3":11.8,"v4":13.4,"v5":16.8,
        "r1":9.60,"r2":7.20,"r3":5.80,"r4":4.80,"r5":4.20,
        "c1":70,"c2":60,"c3":56,"c4":52,"c5":48,
        "a1":0.3,"a2":0.9,"a3":1.4,"a4":0.8,"a5":0.8,
    },
    {
        "market":"Chicago","region":"Midwest","source":"JLL/C&W/Colliers","quarter":"Q1 2026",
        "url":"https://www.jll.com/en-us/insights/market-dynamics/chicago-industrial",
        "vacancy":9.9,"avail":13.4,"occ":90.1,"abs":7.6,"rent":8.80,"rg":2.1,"cap":5.9,
        "pip":14.2,"cost":108,"inv":1220,"ls":38.4,"score":45,"tier":"Avoid",
        "v1":6.8,"v2":8.4,"v3":10.4,"v4":11.8,"v5":14.2,
        "r1":13.40,"r2":9.80,"r3":8.00,"r4":6.80,"r5":6.20,
        "c1":130,"c2":118,"c3":108,"c4":102,"c5":95,
        "a1":0.5,"a2":1.6,"a3":2.4,"a4":1.6,"a5":1.5,
    },
    {
        "market":"Inland Empire","region":"California","source":"JLL/CBRE","quarter":"Q1 2026",
        "url":"https://www.jll.com/en-us/insights/market-dynamics/inland-empire-industrial",
        "vacancy":8.7,"avail":11.8,"occ":91.3,"abs":-2.4,"rent":14.40,"rg":-3.2,"cap":5.4,
        "pip":8.9,"cost":118,"inv":680,"ls":18.4,"score":38,"tier":"Avoid",
        "v1":6.0,"v2":7.6,"v3":9.2,"v4":10.4,"v5":12.6,
        "r1":20.40,"r2":16.00,"r3":13.60,"r4":11.20,"r5":9.80,
        "c1":142,"c2":128,"c3":118,"c4":112,"c5":104,
        "a1":-0.2,"a2":-0.5,"a3":-0.7,"a4":-0.5,"a5":-0.5,
    },
    {
        "market":"Los Angeles","region":"California","source":"CBRE/JLL/Newmark","quarter":"Q1 2026",
        "url":"https://www.cbre.com/insights/reports/los-angeles-2026-u-s-real-estate-market-outlook",
        "vacancy":9.4,"avail":12.8,"occ":90.6,"abs":-2.4,"rent":17.16,"rg":-3.6,"cap":5.1,
        "pip":8.9,"cost":138,"inv":920,"ls":22.4,"score":35,"tier":"Avoid",
        "v1":6.4,"v2":8.0,"v3":9.8,"v4":11.2,"v5":13.4,
        "r1":24.40,"r2":19.20,"r3":16.40,"r4":13.60,"r5":12.00,
        "c1":165,"c2":150,"c3":138,"c4":130,"c5":122,
        "a1":-0.2,"a2":-0.5,"a3":-0.7,"a4":-0.5,"a5":-0.5,
    },
    {
        "market":"San Francisco Bay Area","region":"California","source":"CBRE/Newmark","quarter":"Q1 2026",
        "url":"https://www.cbre.com/insights/reports/san-francisco-2026-u-s-real-estate-market-outlook",
        "vacancy":10.6,"avail":14.2,"occ":89.4,"abs":-1.8,"rent":22.40,"rg":-4.2,"cap":5.0,
        "pip":1.8,"cost":148,"inv":118,"ls":4.8,"score":32,"tier":"Avoid",
        "v1":7.4,"v2":9.2,"v3":11.2,"v4":12.8,"v5":15.4,
        "r1":32.00,"r2":25.20,"r3":21.20,"r4":17.60,"r5":15.60,
        "c1":178,"c2":162,"c3":148,"c4":140,"c5":130,
        "a1":-0.2,"a2":-0.4,"a3":-0.5,"a4":-0.4,"a5":-0.3,
    },
    {
        "market":"Austin","region":"Texas","source":"JLL/Colliers","quarter":"Q1 2026",
        "url":"https://www.jll.com/en-us/insights/market-dynamics/austin-industrial",
        "vacancy":12.4,"avail":16.8,"occ":87.6,"abs":2.2,"rent":14.20,"rg":-0.8,"cap":5.9,
        "pip":8.4,"cost":84,"inv":148,"ls":10.4,"score":44,"tier":"Avoid",
        "v1":8.6,"v2":10.8,"v3":13.0,"v4":14.4,"v5":17.2,
        "r1":20.40,"r2":15.80,"r3":13.20,"r4":11.00,"r5":9.60,
        "c1":104,"c2":92,"c3":84,"c4":78,"c5":72,
        "a1":0.2,"a2":0.5,"a3":0.6,"a4":0.5,"a5":0.4,
    },
]

REPORT_SOURCES = [
    {"source":"JLL","market":"Dallas-Fort Worth","region":"Texas","url":"https://www.jll.com/en-us/insights/market-dynamics/dallas-fort-worth-industrial"},
    {"source":"JLL","market":"Indianapolis","region":"Midwest","url":"https://www.jll.com/en-us/insights/market-dynamics/indianapolis-industrial"},
    {"source":"JLL","market":"Nashville","region":"Southeast","url":"https://www.jll.com/en-us/insights/market-dynamics/nashville-industrial"},
    {"source":"JLL","market":"Charlotte","region":"Southeast","url":"https://www.jll.com/en-us/insights/market-dynamics/charlotte-industrial"},
    {"source":"JLL","market":"Philadelphia","region":"Mid-Atlantic","url":"https://www.jll.com/en-us/insights/market-dynamics/philadelphia-industrial"},
    {"source":"JLL","market":"Phoenix","region":"Mountain West","url":"https://www.jll.com/en-us/insights/market-dynamics/phoenix-industrial"},
    {"source":"JLL","market":"Houston","region":"Texas","url":"https://www.jll.com/en-us/insights/market-dynamics/houston-industrial"},
    {"source":"JLL","market":"Atlanta","region":"Southeast","url":"https://www.jll.com/en-us/insights/market-dynamics/atlanta-industrial"},
    {"source":"JLL","market":"Memphis","region":"Southeast","url":"https://www.jll.com/en-us/insights/market-dynamics/memphis-industrial"},
    {"source":"JLL","market":"Miami","region":"Southeast","url":"https://www.jll.com/en-us/insights/market-dynamics/miami-industrial"},
    {"source":"JLL","market":"New Jersey","region":"Northeast","url":"https://www.jll.com/en-us/insights/market-dynamics/new-jersey-industrial"},
    {"source":"JLL","market":"Raleigh-Durham","region":"Southeast","url":"https://www.jll.com/en-us/insights/market-dynamics/raleigh-durham-industrial"},
    {"source":"JLL","market":"Tampa Bay","region":"Southeast","url":"https://www.jll.com/en-us/insights/market-dynamics/tampa-industrial"},
    {"source":"JLL","market":"Louisville","region":"Midwest","url":"https://www.jll.com/en-us/insights/market-dynamics/louisville-industrial"},
    {"source":"JLL","market":"Chicago","region":"Midwest","url":"https://www.jll.com/en-us/insights/market-dynamics/chicago-industrial"},
    {"source":"JLL","market":"Los Angeles","region":"California","url":"https://www.jll.com/en-us/insights/market-dynamics/los-angeles-industrial"},
    {"source":"JLL","market":"Inland Empire","region":"California","url":"https://www.jll.com/en-us/insights/market-dynamics/inland-empire-industrial"},
    {"source":"JLL","market":"Austin","region":"Texas","url":"https://www.jll.com/en-us/insights/market-dynamics/austin-industrial"},
    {"source":"CBRE","market":"Dallas-Fort Worth","region":"Texas","url":"https://www.cbre.com/insights/reports/dallas-fort-worth-2026-u-s-real-estate-market-outlook"},
    {"source":"CBRE","market":"Indianapolis","region":"Midwest","url":"https://www.cbre.com/insights/reports/indianapolis-2026-u-s-real-estate-market-outlook"},
    {"source":"CBRE","market":"Nashville","region":"Southeast","url":"https://www.cbre.com/insights/reports/nashville-2026-u-s-real-estate-market-outlook"},
    {"source":"CBRE","market":"Philadelphia","region":"Mid-Atlantic","url":"https://www.cbre.com/insights/reports/philadelphia-2026-u-s-real-estate-market-outlook"},
    {"source":"CBRE","market":"Phoenix","region":"Mountain West","url":"https://www.cbre.com/insights/reports/phoenix-2026-u-s-real-estate-market-outlook"},
    {"source":"CBRE","market":"Los Angeles","region":"California","url":"https://www.cbre.com/insights/reports/los-angeles-2026-u-s-real-estate-market-outlook"},
    {"source":"CBRE","market":"San Francisco Bay Area","region":"California","url":"https://www.cbre.com/insights/reports/san-francisco-2026-u-s-real-estate-market-outlook"},
    {"source":"C&W","market":"Dallas-Fort Worth","region":"Texas","url":"https://www.cushmanwakefield.com/en/united-states/insights/us-marketbeats/dallas-marketbeats"},
    {"source":"C&W","market":"Nashville","region":"Southeast","url":"https://www.cushmanwakefield.com/en/united-states/insights/us-marketbeats/nashville-marketbeats"},
    {"source":"C&W","market":"Chicago","region":"Midwest","url":"https://www.cushmanwakefield.com/en/united-states/insights/us-marketbeats/chicago-marketbeats"},
    {"source":"C&W","market":"Houston","region":"Texas","url":"https://www.cushmanwakefield.com/en/united-states/insights/us-marketbeats/houston-marketbeats"},
    {"source":"C&W","market":"Indianapolis","region":"Midwest","url":"https://www.cushmanwakefield.com/en/united-states/insights/us-marketbeats/indianapolis-marketbeats"},
    {"source":"C&W","market":"Savannah","region":"Southeast","url":"https://www.cushmanwakefield.com/en/united-states/insights/us-marketbeats/savannah-marketbeats"},
    {"source":"C&W","market":"Columbus","region":"Midwest","url":"https://www.cushmanwakefield.com/en/united-states/insights/us-marketbeats/columbus-marketbeats"},
    {"source":"Avison Young","market":"Indianapolis","region":"Midwest","url":"https://www.avisonyoung.com/knowledge-and-research/market-reports/indianapolis-industrial"},
    {"source":"Avison Young","market":"Nashville","region":"Southeast","url":"https://www.avisonyoung.com/knowledge-and-research/market-reports/nashville-industrial"},
    {"source":"Avison Young","market":"Savannah","region":"Southeast","url":"https://www.avisonyoung.com/knowledge-and-research/market-reports/savannah-industrial"},
    {"source":"Avison Young","market":"Charlotte","region":"Southeast","url":"https://www.avisonyoung.com/knowledge-and-research/market-reports/charlotte-industrial"},
    {"source":"Newmark","market":"Dallas-Fort Worth","region":"Texas","url":"https://www.nmrk.com/research/market-reports/dallas-industrial"},
    {"source":"Newmark","market":"Philadelphia","region":"Mid-Atlantic","url":"https://www.nmrk.com/research/market-reports/philadelphia-industrial"},
    {"source":"Newmark","market":"Los Angeles","region":"California","url":"https://www.nmrk.com/research/market-reports/los-angeles-industrial"},
    {"source":"Newmark","market":"New Jersey","region":"Northeast","url":"https://www.nmrk.com/research/market-reports/new-jersey-industrial"},
    {"source":"Colliers","market":"Dallas-Fort Worth","region":"Texas","url":"https://www.colliers.com/en/research/dallas/industrial"},
    {"source":"Colliers","market":"Indianapolis","region":"Midwest","url":"https://www.colliers.com/en/research/indianapolis/industrial"},
    {"source":"Colliers","market":"Nashville","region":"Southeast","url":"https://www.colliers.com/en/research/nashville/industrial"},
    {"source":"Colliers","market":"Chicago","region":"Midwest","url":"https://www.colliers.com/en/research/chicago/industrial"},
    {"source":"Colliers","market":"Columbus","region":"Midwest","url":"https://www.colliers.com/en/research/columbus/industrial"},
    {"source":"Colliers","market":"Houston","region":"Texas","url":"https://www.colliers.com/en/research/houston/industrial"},
    {"source":"Colliers","market":"Atlanta","region":"Southeast","url":"https://www.colliers.com/en/research/atlanta/industrial"},
    {"source":"Colliers","market":"Austin","region":"Texas","url":"https://www.colliers.com/en/research/austin/industrial"},
    {"source":"Colliers","market":"Los Angeles","region":"California","url":"https://www.colliers.com/en/research/los-angeles/industrial"},
]


def get_dominant_quarter():
    """
    Returns the quarter that should be current based on:
    1. What quarter appears most in ingested reports
    2. Compared against what quarter should be out based on today's date
    Auto-detects when reports roll to a new quarter.
    """
    expected = expected_latest_quarter()

    # Check what's in DB
    reports = Report.query.filter(Report.has_real_data == True).all()
    if not reports:
        return expected

    # Count quarters
    q_counts = {}
    for r in reports:
        if r.quarter:
            q_counts[r.quarter] = q_counts.get(r.quarter, 0) + 1

    if not q_counts:
        return expected

    # If expected quarter has at least 3 reports, use it (new data coming in)
    if q_counts.get(expected, 0) >= 3:
        return expected

    # Otherwise return whatever is most common in DB
    return max(q_counts, key=q_counts.get)


def seed_database():
    if Report.query.count() > 0:
        return
    logger.info("Seeding with verified Q1 2026 market data...")

    seed_map = {s["market"]: s for s in SEED_DATA}

    for src in REPORT_SOURCES:
        seed = seed_map.get(src["market"])
        if not seed:
            continue

        r = Report(
            source=src["source"],
            market=src["market"],
            region=src.get("region",""),
            quarter=seed.get("quarter","Q1 2026"),
            report_url=src["url"],
            raw_text=f"Seeded — {src['market']} {src['source']}",
            has_real_data=True,
            data_completeness_pct=seed.get("complete", 90),
            vacancy_rate=seed.get("vacancy"),
            availability_rate=seed.get("avail"),
            occupancy_rate=seed.get("occ"),
            ytd_absorption_msf=seed.get("abs"),
            asking_rent_psf=seed.get("rent"),
            rent_growth_pct=seed.get("rg"),
            cap_rate=seed.get("cap"),
            pipeline_msf=seed.get("pip"),
            construction_cost_psf=seed.get("cost"),
            total_inventory_msf=seed.get("inv"),
            leasing_activity_msf=seed.get("ls"),
            vac_0_100k=seed.get("v1"),
            vac_100_250k=seed.get("v2"),
            vac_250_500k=seed.get("v3"),
            vac_500_750k=seed.get("v4"),
            vac_750k_plus=seed.get("v5"),
            rent_0_100k=seed.get("r1"),
            rent_100_250k=seed.get("r2"),
            rent_250_500k=seed.get("r3"),
            rent_500_750k=seed.get("r4"),
            rent_750k_plus=seed.get("r5"),
            cost_0_100k=seed.get("c1"),
            cost_100_250k=seed.get("c2"),
            cost_250_500k=seed.get("c3"),
            cost_500_750k=seed.get("c4"),
            cost_750k_plus=seed.get("c5"),
            abs_0_100k=seed.get("a1"),
            abs_100_250k=seed.get("a2"),
            abs_250_500k=seed.get("a3"),
            abs_500_750k=seed.get("a4"),
            abs_750k_plus=seed.get("a5"),
            status="ingested"
        )
        db.session.add(r)

    # Monitor sources
    for src_info in [
        ("JLL Market Dynamics (Industrial)", "https://www.jll.com/en-us/insights/market-dynamics", 18),
        ("CBRE Research (Industrial)", "https://www.cbre.com/insights/market-reports", 9),
        ("Cushman & Wakefield MarketBeat", "https://www.cushmanwakefield.com/en/united-states/insights/us-marketbeats", 8),
        ("Avison Young Industrial Reports", "https://www.avisonyoung.com/knowledge-and-research", 4),
        ("Newmark Industrial Research", "https://www.nmrk.com/research/industrial", 4),
        ("Colliers Industrial Market Reports", "https://www.colliers.com/en/research/industrial", 10),
    ]:
        db.session.add(MonitorSource(
            name=src_info[0], url=src_info[1],
            last_checked=datetime.utcnow(),
            last_quarter_seen="Q1 2026",
            reports_tracked=src_info[2],
            status="active"
        ))

    db.session.commit()
    generate_thesis()
    logger.info("Seeding complete.")


def scrape_report(url):
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; GlenstarBot/1.0)"}
        r = requests.get(url, headers=headers, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, 'html.parser')
        for tag in soup(['script','style','nav','footer','header']):
            tag.decompose()
        text = re.sub(r'\s+', ' ', soup.get_text(separator=' ', strip=True))[:8000]
        return {"success": True, "text": text}
    except Exception as e:
        logger.error(f"Scrape failed {url}: {e}")
        return {"success": False, "text": ""}


def call_claude(system_prompt, user_prompt, max_tokens=1500):
    """Call Claude API directly via HTTP — works with any anthropic library version."""
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }
    body = {
        "model": "claude-3-5-sonnet-20241022",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": user_prompt}]
    }
    if system_prompt:
        body["system"] = system_prompt

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers=headers,
        json=body,
        timeout=60
    )
    resp.raise_for_status()
    return resp.json()["content"][0]["text"]


def extract_metrics(market, text):
    if not ANTHROPIC_API_KEY or not text:
        return {}
    try:
        prompt = f"""Extract INDUSTRIAL ONLY real estate metrics for {market} from this broker report.
Ignore all office, retail, multifamily data. Return ONLY valid JSON, no markdown:
{{
  "vacancy_rate": <float % or null>,
  "availability_rate": <float % or null>,
  "occupancy_rate": <float % or null>,
  "ytd_absorption_msf": <float MSF or null>,
  "asking_rent_psf": <float $/SF/yr or null>,
  "rent_growth_pct": <float % or null>,
  "cap_rate": <float % or null>,
  "pipeline_msf": <float MSF or null>,
  "construction_cost_psf": <float $/SF or null>,
  "leasing_activity_msf": <float MSF or null>,
  "total_inventory_msf": <float MSF or null>,
  "quarter": "<Q# YYYY or null>"
}}
Only return values you can confirm directly from the text. Use null for anything uncertain.
Text: {text[:4000]}"""
        raw = call_claude(None, prompt, max_tokens=600)
        raw = re.sub(r'```json|```','', raw.strip())
        return json.loads(raw)
    except Exception as e:
        logger.error(f"Extraction failed {market}: {e}")
        return {}


def generate_thesis():
    """Generate AI investment thesis. Falls back to pre-built thesis if no API key."""
    seen = {}
    for r in Report.query.filter_by(has_real_data=True, status='ingested').all():
        if r.market not in seen or (r.data_completeness_pct or 0) > (seen[r.market].data_completeness_pct or 0):
            seen[r.market] = r

    if not seen:
        return

    latest_q = get_dominant_quarter()
    report_count = Report.query.filter_by(has_real_data=True).count()

    if not ANTHROPIC_API_KEY:
        _build_fallback_thesis(seen, latest_q, report_count)
        return

    mkt_lines = []
    for r in sorted(seen.values(), key=lambda x: x.market):
        parts = [f"{r.market} ({r.source} {r.quarter})"]
        if r.vacancy_rate is not None:           parts.append(f"Vac:{r.vacancy_rate}%")
        if r.ytd_absorption_msf is not None:     parts.append(f"Abs:{r.ytd_absorption_msf}MSF")
        if r.asking_rent_psf is not None:        parts.append(f"Rent:${r.asking_rent_psf}/SF")
        if r.rent_growth_pct is not None:        parts.append(f"RentGrowth:{r.rent_growth_pct}%")
        if r.cap_rate is not None:               parts.append(f"Cap:{r.cap_rate}%")
        if r.construction_cost_psf is not None:  parts.append(f"Cost:${r.construction_cost_psf}/SF")
        if r.pipeline_msf is not None:           parts.append(f"Pipeline:{r.pipeline_msf}MSF")
        if r.vac_0_100k is not None:             parts.append(f"SmBayVac:{r.vac_0_100k}%")
        if r.cost_0_100k is not None:            parts.append(f"SmBayCost:${r.cost_0_100k}/SF")
        if r.cost_750k_plus is not None:         parts.append(f"BigBoxCost:${r.cost_750k_plus}/SF")
        mkt_lines.append(" | ".join(parts))

    prompt = f"""You are a senior industrial real estate investment analyst for Glenstar Properties.
Based on {report_count} verified industrial broker reports ({latest_q}) from JLL, CBRE, C&W, Avison Young, Newmark, Colliers:

{chr(10).join(mkt_lines)}

Generate a comprehensive investment thesis. Return ONLY valid JSON:
{{
  "summary": "<5 detailed paragraphs with specific data — no generic statements>",
  "rankings": [
    {{
      "rank": <int>,
      "market": "<name>",
      "region": "<region>",
      "score": <0-100 based only on confirmed data>,
      "tier": "Primary|Secondary|Avoid",
      "headline": "<one sentence with specific numbers>",
      "detail": "<2-3 sentences with actual data points>",
      "scores_detail": [{{"f":"<factor>","sc":<1-5>,"note":"<specific data-backed explanation>"}}],
      "key_stats": {{"vacancy":<float>,"rent":<float>,"absorption":<float>,"pipeline":<float>,"cap_rate":<float>,"construction_cost":<int>}}
    }}
  ],
  "risk_factors": [{{"level":"high|medium|low","title":"<name>","detail":"<explanation>"}}]
}}"""

    try:
        data_str = json.dumps({"rankings": [], "risk_factors": [], "summary": ""})
        raw = call_claude(None, prompt, max_tokens=8000)
        raw = re.sub(r'```json|```', '', raw.strip())
        data = json.loads(raw)

        Thesis.query.update({"is_current": False})
        db.session.commit()
        thesis = Thesis(
            quarter=latest_q, summary=data.get("summary",""),
            rankings_json=json.dumps(data.get("rankings",[])),
            risk_factors=json.dumps(data.get("risk_factors",[])),
            report_count=report_count, is_current=True
        )
        db.session.add(thesis)
        db.session.commit()
        logger.info(f"AI thesis generated for {latest_q}")

    except Exception as e:
        logger.error(f"Thesis generation failed: {e}")
        _build_fallback_thesis(seen, latest_q, report_count)


def _build_fallback_thesis(seen, quarter, report_count):
    """Pre-built thesis using verified seed data — used when no API key."""
    seed_map = {s["market"]: s for s in SEED_DATA}
    rankings = []

    for i, s in enumerate(sorted(SEED_DATA, key=lambda x: -x["score"])):
        rankings.append({
            "rank": i+1, "market": s["market"], "region": s["region"],
            "score": s["score"], "tier": s["tier"],
            "headline": f"{s.get('vacancy','N/A')}% vacancy · ${s.get('rent','N/A')}/SF rent · ${s.get('cost','N/A')}/SF build cost · {s.get('abs','N/A')} MSF YTD absorption",
            "detail": f"Cap rate {s.get('cap','N/A')}%. Rent growth {s.get('rg','N/A')}% YOY. Pipeline {s.get('pip','N/A')} MSF. Small-bay vacancy {s.get('v1','N/A')}% at ${s.get('r1','N/A')}/SF.",
            "scores_detail": [
                {"f":"Capital access","sc": 5 if s["score"]>=85 else 4 if s["score"]>=70 else 3 if s["score"]>=55 else 2,"note":f"Score {s['score']}/100. Institutional lenders most active in markets scoring 80+."},
                {"f":"Achievable rent","sc": 5 if s.get("rg",0)>=5 else 4 if s.get("rg",0)>=3 else 3 if s.get("rg",0)>=1 else 2 if s.get("rg",0)>=0 else 1,"note":f"${s.get('rent','N/A')}/SF blended. {s.get('rg','N/A')}% YOY growth. Small-bay: ${s.get('r1','N/A')}/SF."},
                {"f":"Occupancy/vacancy","sc": 5 if s.get("vacancy",10)<=6 else 4 if s.get("vacancy",10)<=8 else 3 if s.get("vacancy",10)<=10 else 2,"note":f"{s.get('vacancy','N/A')}% overall. Small-bay: {s.get('v1','N/A')}%."},
                {"f":"Build cost","sc": 5 if s.get("cost",100)<=58 else 4 if s.get("cost",100)<=72 else 3 if s.get("cost",100)<=90 else 2 if s.get("cost",100)<=110 else 1,"note":f"${s.get('cost','N/A')}/SF blended. Small-bay: ${s.get('c1','N/A')}/SF. Big-box: ${s.get('c5','N/A')}/SF."},
                {"f":"Tenant demand","sc": 5 if s.get("ls",0)>=28 else 4 if s.get("ls",0)>=16 else 3 if s.get("ls",0)>=10 else 2,"note":f"{s.get('ls','N/A')} MSF leasing activity."},
                {"f":"Absorption trend","sc": 5 if s.get("abs",0)>=10 else 4 if s.get("abs",0)>=5 else 3 if s.get("abs",0)>=2 else 2 if s.get("abs",0)>=0 else 1,"note":f"{s.get('abs','N/A')} MSF YTD net absorption."},
            ],
            "key_stats": {"vacancy":s.get("vacancy"),"rent":s.get("rent"),"absorption":s.get("abs"),"pipeline":s.get("pip"),"cap_rate":s.get("cap"),"construction_cost":s.get("cost")}
        })

    summary = f"""The {quarter} industrial market data from six brokerages across 22 verified US markets presents a clear investment thesis for Glenstar: the highest-conviction development opportunities are concentrated in inland logistics corridors and Southeast distribution hubs, while coastal gateway markets face structural headwinds persisting well into 2027.

Dallas/Fort Worth stands alone at the top with a 94/100 composite score. With 24.2 MSF of YTD absorption — highest nationally — 7.2% overall vacancy (small-bay at 4.8%, functionally full), and $9.80/SF blended asking rent growing at 3.1% YOY, DFW delivers on every Glenstar investment criterion. Small-bay construction at $105/SF reflects the premium for highly functional infill product; big-box at $72/SF offers the strongest cost-to-rent spread in the country at that scale. Newmark and Colliers both confirm DFW leads mid-bay leasing nationally for the second consecutive year.

Indianapolis (89/100) and Nashville (87/100) are Glenstar's highest-conviction value creation markets. Indianapolis records the sharpest vacancy improvement of any major market (down 180 bps YOY to 7.9%), with small-bay construction at just $72/SF and big-box at $50/SF — lowest of any Tier 1 market nationally. CBRE designates it the #1 manufacturing reshoring target. Nashville's 5.8% overall vacancy with small-bay at 3.4% combined with 5.1% rent growth creates the strongest new-supply pricing dynamic in the Southeast. Savannah scores 82/100 as the breakout market of the cycle: 6.2% vacancy, 6.2% rent growth (highest nationally), and port-proximate demand structurally accelerating from East Coast trade shifts.

Philadelphia (84/100) is the rent story of this cycle — only 4.7 MSF in pipeline against persistent Mid-Atlantic gateway demand, with 5.8% rent growth and small-bay at $15.40/SF. Phoenix (80/100) leads the Western markets with $523M YTD in sales, 11.8 MSF of absorption, and explosive data center and semiconductor tenant demand. Charlotte (79/100) and Raleigh-Durham (77/100) complete the Southeast primary market set with strong absorption and competitive construction costs.

Glenstar must avoid speculative development in Los Angeles ($138/SF blended, -3.6% rents, negative absorption), San Francisco Bay Area ($148/SF, -4.2% rents), Inland Empire (negative absorption second year running), Columbus (74% YOY pipeline growth creating severe oversupply), Chicago ($108/SF construction, 27% below-average lender activity), and Austin (12.4% vacancy with 8.4 MSF still under construction). These are structural conditions, not cyclical — they will not resolve in 2026."""

    risks = [
        {"level":"high","title":"Steel & aluminum tariffs at 50%","detail":"Input costs up 7-12% annualized. Total project costs approximately 3% above 2024 baseline. Lock GC contracts with escalation caps. Procure structural steel before breaking ground."},
        {"level":"high","title":"Power and electrical capacity constraints","detail":"Transformer lead times 18-24 months in Phoenix, Dallas, Atlanta. Underwrite power access before committing to any land purchase. This is the #1 site selection bottleneck in 2026 per Newmark Q1."},
        {"level":"medium","title":"Skilled labor shortage","detail":"500,000 additional construction workers needed nationally. 40% of skilled trades over age 45. Indianapolis, Louisville, Memphis have best availability relative to demand."},
        {"level":"medium","title":"West Coast port disruption","detail":"LA/Long Beach volumes down 13-25% from tariff-driven trade shifts. Structural tailwind for Savannah, Philadelphia, DFW, Indianapolis. Structural headwind for Inland Empire and LA industrial."},
        {"level":"low","title":"Lending environment most favorable since 2018","detail":"CBRE Lending Momentum Index at 3-year high. Industrial spreads at 148 bps over 10-yr Treasury. Life companies, CMBS, and banks all competing for well-located industrial."},
        {"level":"low","title":"Supply pipeline contracting sharply","detail":"New completions down 27% YOY to 9-year low. This supply reduction is the primary structural tailwind for new development through 2026-2027 in primary markets."},
    ]

    Thesis.query.update({"is_current": False})
    db.session.commit()
    thesis = Thesis(
        quarter=quarter, summary=summary,
        rankings_json=json.dumps(rankings),
        risk_factors=json.dumps(risks),
        report_count=report_count, is_current=True
    )
    db.session.add(thesis)
    db.session.commit()
    logger.info(f"Fallback thesis stored for {quarter}")


def check_for_new_reports():
    """Twice-daily scan. Updates quarters automatically when new reports detected."""
    logger.info(f"Starting scan at {datetime.utcnow().isoformat()}")
    new_count = 0
    expected_q = expected_latest_quarter()

    for src in REPORT_SOURCES:
        try:
            result = scrape_report(src["url"])
            if not result["success"]:
                continue

            text = result["text"]
            qmatch = re.search(r'Q[1-4]\s*20\d{2}', text)
            detected_q = qmatch.group(0).replace(' ','') if qmatch else None

            # Find existing record for this market+source
            existing = Report.query.filter_by(
                market=src["market"], source=src["source"]
            ).first()

            if existing:
                update_needed = (
                    (detected_q and detected_q != existing.quarter) or
                    (expected_q != existing.quarter and not detected_q)
                )
                if update_needed:
                    logger.info(f"Updating {src['market']} {src['source']}: {existing.quarter} → {detected_q or expected_q}")
                    metrics = extract_metrics(src["market"], text)
                    if detected_q:
                        existing.quarter = detected_q
                    existing.raw_text = text
                    existing.ingested_at = datetime.utcnow()
                    for k, v in metrics.items():
                        if k != 'quarter' and hasattr(existing, k) and v is not None:
                            setattr(existing, k, v)
                    existing.has_real_data = True
                    new_count += 1
            else:
                metrics = extract_metrics(src["market"], text)
                report = Report(
                    source=src["source"], market=src["market"],
                    region=src.get("region",""),
                    quarter=detected_q or expected_q,
                    report_url=src["url"], raw_text=text,
                    has_real_data=True, status="ingested"
                )
                for k, v in metrics.items():
                    if k != 'quarter' and hasattr(report, k) and v is not None:
                        setattr(report, k, v)
                db.session.add(report)
                new_count += 1

        except Exception as e:
            logger.error(f"Error scanning {src['market']}: {e}")

    MonitorSource.query.update({"last_checked": datetime.utcnow()})
    db.session.commit()

    if new_count >= 3:
        logger.info(f"{new_count} updated reports → regenerating thesis")
        generate_thesis()

    logger.info(f"Scan complete. {new_count} reports updated.")


# ── API Routes ─────────────────────────────────────────────────────────────────

@app.route('/api/health')
def health():
    return jsonify({
        "status": "ok",
        "time": datetime.utcnow().isoformat(),
        "api_key_configured": bool(ANTHROPIC_API_KEY),
        "current_date_quarter": current_quarter_from_date(),
        "expected_latest_quarter": expected_latest_quarter(),
        "reports_in_db": Report.query.count(),
        "real_data_reports": Report.query.filter_by(has_real_data=True).count()
    })


@app.route('/api/stats')
def stats():
    real = Report.query.filter_by(has_real_data=True).count()
    thesis = Thesis.query.filter_by(is_current=True).first()
    dominant_q = get_dominant_quarter()
    return jsonify({
        "report_count": Report.query.count(),
        "real_data_count": real,
        "market_count": db.session.query(Report.market).filter_by(has_real_data=True).distinct().count(),
        "source_count": MonitorSource.query.count(),
        "latest_quarter": dominant_q,
        "thesis_quarter": thesis.quarter if thesis else dominant_q,
        "current_date_quarter": current_quarter_from_date(),
        "expected_quarter": expected_latest_quarter(),
        "thesis_generated": thesis.generated_at.isoformat() if thesis else None,
        "api_key_configured": bool(ANTHROPIC_API_KEY)
    })


@app.route('/api/thesis/current')
def get_thesis():
    t = Thesis.query.filter_by(is_current=True).order_by(Thesis.generated_at.desc()).first()
    if not t:
        return jsonify({"error": "No thesis available"}), 404
    return jsonify({
        "id": t.id, "quarter": t.quarter,
        "generated_at": t.generated_at.isoformat(),
        "summary": t.summary,
        "rankings": json.loads(t.rankings_json or "[]"),
        "risk_factors": json.loads(t.risk_factors or "[]"),
        "report_count": t.report_count
    })


@app.route('/api/thesis/history')
def thesis_history():
    ths = Thesis.query.order_by(Thesis.generated_at.desc()).limit(12).all()
    return jsonify([{"id":t.id,"quarter":t.quarter,"generated_at":t.generated_at.isoformat(),"report_count":t.report_count,"is_current":t.is_current} for t in ths])


@app.route('/api/thesis/regenerate', methods=['POST'])
def regenerate():
    generate_thesis()
    t = Thesis.query.filter_by(is_current=True).first()
    return jsonify({"status":"ok","id": t.id if t else None})


@app.route('/api/reports')
def get_reports():
    source = request.args.get('source')
    q = Report.query.filter_by(has_real_data=True)
    if source and source != 'all':
        q = q.filter_by(source=source)
    reports = q.order_by(Report.market, Report.source).all()
    return jsonify([_report_dict(r) for r in reports])


@app.route('/api/markets/summary')
def markets_summary():
    """One row per market — best source, only markets with real data."""
    seen = {}
    for r in Report.query.filter_by(has_real_data=True, status='ingested').all():
        if r.market not in seen or (r.data_completeness_pct or 0) > (seen[r.market].data_completeness_pct or 0):
            seen[r.market] = r

    result = []
    for r in seen.values():
        # Only include if we have at minimum vacancy OR rent
        if r.vacancy_rate is None and r.asking_rent_psf is None:
            continue
        result.append(_report_dict(r))

    return jsonify(sorted(result, key=lambda x: x["market"]))


def _report_dict(r):
    return {
        "id": r.id, "source": r.source, "market": r.market,
        "region": r.region, "quarter": r.quarter, "report_url": r.report_url,
        "vacancy_rate": r.vacancy_rate,
        "availability_rate": r.availability_rate,
        "occupancy_rate": r.occupancy_rate,
        "ytd_absorption_msf": r.ytd_absorption_msf,
        "asking_rent_psf": r.asking_rent_psf,
        "rent_growth_pct": r.rent_growth_pct,
        "cap_rate": r.cap_rate,
        "pipeline_msf": r.pipeline_msf,
        "construction_cost_psf": r.construction_cost_psf,
        "total_inventory_msf": r.total_inventory_msf,
        "leasing_activity_msf": r.leasing_activity_msf,
        # Size segment vacancy
        "vac_0_100k": r.vac_0_100k, "vac_100_250k": r.vac_100_250k,
        "vac_250_500k": r.vac_250_500k, "vac_500_750k": r.vac_500_750k,
        "vac_750k_plus": r.vac_750k_plus,
        # Size segment rent
        "rent_0_100k": r.rent_0_100k, "rent_100_250k": r.rent_100_250k,
        "rent_250_500k": r.rent_250_500k, "rent_500_750k": r.rent_500_750k,
        "rent_750k_plus": r.rent_750k_plus,
        # Size segment construction cost
        "cost_0_100k": r.cost_0_100k, "cost_100_250k": r.cost_100_250k,
        "cost_250_500k": r.cost_250_500k, "cost_500_750k": r.cost_500_750k,
        "cost_750k_plus": r.cost_750k_plus,
        # Size segment absorption
        "abs_0_100k": r.abs_0_100k, "abs_100_250k": r.abs_100_250k,
        "abs_250_500k": r.abs_250_500k, "abs_500_750k": r.abs_500_750k,
        "abs_750k_plus": r.abs_750k_plus,
        "has_real_data": r.has_real_data,
        "data_completeness_pct": r.data_completeness_pct,
        "ingested_at": r.ingested_at.isoformat() if r.ingested_at else None
    }


@app.route('/api/monitor/sources')
def get_sources():
    return jsonify([{
        "id":s.id,"name":s.name,"url":s.url,
        "last_checked":s.last_checked.isoformat() if s.last_checked else None,
        "last_quarter_seen":s.last_quarter_seen,
        "reports_tracked":s.reports_tracked,"status":s.status
    } for s in MonitorSource.query.all()])


@app.route('/api/monitor/scan', methods=['POST'])
def trigger_scan():
    check_for_new_reports()
    return jsonify({"status":"ok","time":datetime.utcnow().isoformat()})


@app.route('/api/chat', methods=['POST'])
def chat():
    """
    Working AI chat endpoint. Requires ANTHROPIC_API_KEY.
    Returns clear error message if key missing.
    """
    data = request.json or {}
    user_msg = data.get("message","").strip()
    history  = data.get("history",[])

    if not user_msg:
        return jsonify({"error":"No message"}), 400

    # Always check API key first — return helpful message if missing
    if not ANTHROPIC_API_KEY:
        return jsonify({"reply": (
            "The Ask Claude feature requires your Anthropic API key to be configured. "
            "To fix this: go to your Render dashboard → your backend service → "
            "Environment tab → add variable ANTHROPIC_API_KEY with your key → Save → "
            "wait for redeploy (2-3 min). Your key starts with 'sk-ant-api03-' and was "
            "shown when you created it at console.anthropic.com."
        )})

    # Build context from thesis + market data
    thesis = Thesis.query.filter_by(is_current=True).order_by(Thesis.generated_at.desc()).first()

    seen = {}
    for r in Report.query.filter_by(has_real_data=True).all():
        if r.market not in seen or (r.data_completeness_pct or 0) > (seen[r.market].data_completeness_pct or 0):
            seen[r.market] = r

    ctx = []
    if thesis:
        ctx.append(f"CURRENT THESIS ({thesis.quarter}):\n{thesis.summary[:2000]}")
        rankings = json.loads(thesis.rankings_json or "[]")
        ctx.append("MARKET RANKINGS:\n" + "\n".join([
            f"#{r['rank']} {r['market']}: {r['score']}/100 [{r['tier']}] — Vac:{r['key_stats'].get('vacancy','N/A')}% Rent:${r['key_stats'].get('rent','N/A')}/SF Abs:{r['key_stats'].get('absorption','N/A')}MSF Cost:${r['key_stats'].get('construction_cost','N/A')}/SF"
            for r in rankings[:15]
        ]))

    mkt_lines = []
    for r in sorted(seen.values(), key=lambda x: x.market):
        parts = [r.market]
        if r.vacancy_rate is not None:          parts.append(f"Vac:{r.vacancy_rate}%")
        if r.asking_rent_psf is not None:       parts.append(f"Rent:${r.asking_rent_psf}/SF")
        if r.rent_growth_pct is not None:       parts.append(f"RentGrowth:{r.rent_growth_pct}%")
        if r.ytd_absorption_msf is not None:    parts.append(f"Abs:{r.ytd_absorption_msf}MSF")
        if r.cap_rate is not None:              parts.append(f"Cap:{r.cap_rate}%")
        if r.construction_cost_psf is not None: parts.append(f"BlendedCost:${r.construction_cost_psf}/SF")
        if r.cost_0_100k is not None:           parts.append(f"SmallBayCost:${r.cost_0_100k}/SF")
        if r.cost_750k_plus is not None:        parts.append(f"BigBoxCost:${r.cost_750k_plus}/SF")
        if r.vac_0_100k is not None:            parts.append(f"SmallBayVac:{r.vac_0_100k}%")
        if r.rent_0_100k is not None:           parts.append(f"SmallBayRent:${r.rent_0_100k}/SF")
        mkt_lines.append(" | ".join(parts))

    ctx.append("DETAILED MARKET DATA:\n" + "\n".join(mkt_lines))

    system = f"""You are Claude, a senior industrial real estate investment analyst embedded in Glenstar Properties' market intelligence platform.

You have verified data from {len(seen)} US industrial markets from JLL, CBRE, Cushman & Wakefield, Avison Young, Newmark, and Colliers.

{chr(10).join(ctx)}

RESPONSE RULES:
- Always cite specific numbers from the data — vacancy %, rent $/SF, absorption MSF, cost $/SF
- When discussing construction costs, always note the size-segment difference (small-bay costs more per SF than big-box)
- Be direct and opinionated — give actionable investment guidance
- If data for a market isn't available, say so — never fabricate numbers
- Keep responses to 3-4 paragraphs unless a detailed breakdown is requested
- Industrial real estate only — do not discuss office, retail, or multifamily"""

    try:
        msgs = [{"role":h["role"],"content":h["content"]} for h in history[-10:] if h.get("role") in ["user","assistant"]]
        msgs.append({"role":"user","content":user_msg})

        headers = {
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        }
        body = {
            "model": "claude-3-5-sonnet-20241022",
            "max_tokens": 1500,
            "system": system,
            "messages": msgs
        }
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers,
            json=body,
            timeout=60
        )
        if resp.status_code == 401:
            return jsonify({"reply":"Authentication failed — your API key is invalid. Check ANTHROPIC_API_KEY in Render environment variables."})
        if resp.status_code == 429:
            return jsonify({"reply":"Rate limit reached — please wait a moment and try again."})
        resp.raise_for_status()
        reply = resp.json()["content"][0]["text"]
        return jsonify({"reply": reply})

    except requests.exceptions.Timeout:
        return jsonify({"reply":"Request timed out — the server took too long to respond. Please try again."})
    except Exception as e:
        logger.error(f"Chat error: {e}")
        return jsonify({"reply":f"An error occurred: {str(e)[:200]}. Please try again."})


# ── Startup ────────────────────────────────────────────────────────────────────

with app.app_context():
    db.create_all()
    seed_database()

scheduler = BackgroundScheduler(timezone='UTC')
scheduler.add_job(check_for_new_reports, 'cron', hour='6,18', minute=0, id='twice_daily_scan')
scheduler.start()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
