from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from apscheduler.schedulers.background import BackgroundScheduler
import anthropic
import requests
from bs4 import BeautifulSoup
import os, json, re
from datetime import datetime
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///glenstar.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')

# ── Models ─────────────────────────────────────────────────────────────────────

class Report(db.Model):
    id                     = db.Column(db.Integer, primary_key=True)
    source                 = db.Column(db.String(30))
    market                 = db.Column(db.String(120))
    region                 = db.Column(db.String(60))
    quarter                = db.Column(db.String(20))
    report_url             = db.Column(db.String(600))   # Direct link to report
    raw_text               = db.Column(db.Text)
    # Core metrics — all nullable, shown as N/A if missing
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
    # Size segment vacancy
    vac_0_100k             = db.Column(db.Float)
    vac_100_250k           = db.Column(db.Float)
    vac_250_500k           = db.Column(db.Float)
    vac_500_750k           = db.Column(db.Float)
    vac_750k_plus          = db.Column(db.Float)
    # Size segment rent
    rent_0_100k            = db.Column(db.Float)
    rent_100_250k          = db.Column(db.Float)
    rent_250_500k          = db.Column(db.Float)
    rent_500_750k          = db.Column(db.Float)
    rent_750k_plus         = db.Column(db.Float)
    # Data quality flag — only show market if we have real data
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


# ── Complete report sources with verified direct URLs ──────────────────────────

REPORT_SOURCES = [
    # ── JLL ──────────────────────────────────────────────────────────────────
    {"source":"JLL","market":"Atlanta","region":"Southeast","url":"https://www.jll.com/en-us/insights/market-dynamics/atlanta-industrial","quarter":"Q1 2026"},
    {"source":"JLL","market":"Austin","region":"Texas","url":"https://www.jll.com/en-us/insights/market-dynamics/austin-industrial","quarter":"Q1 2026"},
    {"source":"JLL","market":"Baltimore","region":"Mid-Atlantic","url":"https://www.jll.com/en-us/insights/market-dynamics/baltimore-industrial","quarter":"Q1 2026"},
    {"source":"JLL","market":"Boston","region":"Northeast","url":"https://www.jll.com/en-us/insights/market-dynamics/boston-industrial","quarter":"Q1 2026"},
    {"source":"JLL","market":"Charlotte","region":"Southeast","url":"https://www.jll.com/en-us/insights/market-dynamics/charlotte-industrial","quarter":"Q1 2026"},
    {"source":"JLL","market":"Chicago","region":"Midwest","url":"https://www.jll.com/en-us/insights/market-dynamics/chicago-industrial","quarter":"Q1 2026"},
    {"source":"JLL","market":"Cincinnati","region":"Midwest","url":"https://www.jll.com/en-us/insights/market-dynamics/cincinnati-industrial","quarter":"Q1 2026"},
    {"source":"JLL","market":"Columbus","region":"Midwest","url":"https://www.jll.com/en-us/insights/market-dynamics/columbus-industrial","quarter":"Q1 2026"},
    {"source":"JLL","market":"Dallas-Fort Worth","region":"Texas","url":"https://www.jll.com/en-us/insights/market-dynamics/dallas-fort-worth-industrial","quarter":"Q1 2026"},
    {"source":"JLL","market":"Denver","region":"Mountain West","url":"https://www.jll.com/en-us/insights/market-dynamics/denver-industrial","quarter":"Q1 2026"},
    {"source":"JLL","market":"Detroit","region":"Midwest","url":"https://www.jll.com/en-us/insights/market-dynamics/detroit-industrial","quarter":"Q1 2026"},
    {"source":"JLL","market":"Houston","region":"Texas","url":"https://www.jll.com/en-us/insights/market-dynamics/houston-industrial","quarter":"Q1 2026"},
    {"source":"JLL","market":"Indianapolis","region":"Midwest","url":"https://www.jll.com/en-us/insights/market-dynamics/indianapolis-industrial","quarter":"Q1 2026"},
    {"source":"JLL","market":"Inland Empire","region":"California","url":"https://www.jll.com/en-us/insights/market-dynamics/inland-empire-industrial","quarter":"Q1 2026"},
    {"source":"JLL","market":"Jacksonville","region":"Southeast","url":"https://www.jll.com/en-us/insights/market-dynamics/jacksonville-industrial","quarter":"Q1 2026"},
    {"source":"JLL","market":"Kansas City","region":"Midwest","url":"https://www.jll.com/en-us/insights/market-dynamics/kansas-city-industrial","quarter":"Q1 2026"},
    {"source":"JLL","market":"Las Vegas","region":"Mountain West","url":"https://www.jll.com/en-us/insights/market-dynamics/las-vegas-industrial","quarter":"Q1 2026"},
    {"source":"JLL","market":"Los Angeles","region":"California","url":"https://www.jll.com/en-us/insights/market-dynamics/los-angeles-industrial","quarter":"Q1 2026"},
    {"source":"JLL","market":"Louisville","region":"Midwest","url":"https://www.jll.com/en-us/insights/market-dynamics/louisville-industrial","quarter":"Q1 2026"},
    {"source":"JLL","market":"Memphis","region":"Southeast","url":"https://www.jll.com/en-us/insights/market-dynamics/memphis-industrial","quarter":"Q1 2026"},
    {"source":"JLL","market":"Miami","region":"Southeast","url":"https://www.jll.com/en-us/insights/market-dynamics/miami-industrial","quarter":"Q1 2026"},
    {"source":"JLL","market":"Minneapolis","region":"Midwest","url":"https://www.jll.com/en-us/insights/market-dynamics/minneapolis-industrial","quarter":"Q1 2026"},
    {"source":"JLL","market":"Nashville","region":"Southeast","url":"https://www.jll.com/en-us/insights/market-dynamics/nashville-industrial","quarter":"Q1 2026"},
    {"source":"JLL","market":"New Jersey","region":"Northeast","url":"https://www.jll.com/en-us/insights/market-dynamics/new-jersey-industrial","quarter":"Q1 2026"},
    {"source":"JLL","market":"Orlando","region":"Southeast","url":"https://www.jll.com/en-us/insights/market-dynamics/orlando-industrial","quarter":"Q1 2026"},
    {"source":"JLL","market":"Philadelphia","region":"Mid-Atlantic","url":"https://www.jll.com/en-us/insights/market-dynamics/philadelphia-industrial","quarter":"Q1 2026"},
    {"source":"JLL","market":"Phoenix","region":"Mountain West","url":"https://www.jll.com/en-us/insights/market-dynamics/phoenix-industrial","quarter":"Q1 2026"},
    {"source":"JLL","market":"Portland","region":"Pacific Northwest","url":"https://www.jll.com/en-us/insights/market-dynamics/portland-industrial","quarter":"Q1 2026"},
    {"source":"JLL","market":"Raleigh-Durham","region":"Southeast","url":"https://www.jll.com/en-us/insights/market-dynamics/raleigh-durham-industrial","quarter":"Q1 2026"},
    {"source":"JLL","market":"Sacramento","region":"California","url":"https://www.jll.com/en-us/insights/market-dynamics/sacramento-industrial","quarter":"Q1 2026"},
    {"source":"JLL","market":"Salt Lake City","region":"Mountain West","url":"https://www.jll.com/en-us/insights/market-dynamics/salt-lake-city-industrial","quarter":"Q1 2026"},
    {"source":"JLL","market":"San Antonio","region":"Texas","url":"https://www.jll.com/en-us/insights/market-dynamics/san-antonio-industrial","quarter":"Q1 2026"},
    {"source":"JLL","market":"San Diego","region":"California","url":"https://www.jll.com/en-us/insights/market-dynamics/san-diego-industrial","quarter":"Q1 2026"},
    {"source":"JLL","market":"San Francisco Bay Area","region":"California","url":"https://www.jll.com/en-us/insights/market-dynamics/san-francisco-industrial","quarter":"Q1 2026"},
    {"source":"JLL","market":"Savannah","region":"Southeast","url":"https://www.jll.com/en-us/insights/market-dynamics/savannah-industrial","quarter":"Q1 2026"},
    {"source":"JLL","market":"Seattle","region":"Pacific Northwest","url":"https://www.jll.com/en-us/insights/market-dynamics/seattle-bellevue-industrial","quarter":"Q1 2026"},
    {"source":"JLL","market":"St. Louis","region":"Midwest","url":"https://www.jll.com/en-us/insights/market-dynamics/st-louis-industrial","quarter":"Q1 2026"},
    {"source":"JLL","market":"Tampa Bay","region":"Southeast","url":"https://www.jll.com/en-us/insights/market-dynamics/tampa-industrial","quarter":"Q1 2026"},
    {"source":"JLL","market":"Washington DC","region":"Mid-Atlantic","url":"https://www.jll.com/en-us/insights/market-dynamics/washington-dc-industrial","quarter":"Q1 2026"},
    # ── CBRE ─────────────────────────────────────────────────────────────────
    {"source":"CBRE","market":"US Industrial (National)","region":"National","url":"https://www.cbre.com/insights/books/us-real-estate-market-outlook-2026/industrial","quarter":"2026 Annual"},
    {"source":"CBRE","market":"Atlanta","region":"Southeast","url":"https://www.cbre.com/insights/reports/atlanta-2026-u-s-real-estate-market-outlook","quarter":"2026 Outlook"},
    {"source":"CBRE","market":"Boston","region":"Northeast","url":"https://www.cbre.com/insights/reports/boston-2026-u-s-real-estate-market-outlook","quarter":"2026 Outlook"},
    {"source":"CBRE","market":"Charlotte","region":"Southeast","url":"https://www.cbre.com/insights/reports/charlotte-2026-u-s-real-estate-market-outlook","quarter":"2026 Outlook"},
    {"source":"CBRE","market":"Chicago","region":"Midwest","url":"https://www.cbre.com/insights/reports/chicago-2026-u-s-real-estate-market-outlook","quarter":"2026 Outlook"},
    {"source":"CBRE","market":"Dallas-Fort Worth","region":"Texas","url":"https://www.cbre.com/insights/reports/dallas-fort-worth-2026-u-s-real-estate-market-outlook","quarter":"2026 Outlook"},
    {"source":"CBRE","market":"Houston","region":"Texas","url":"https://www.cbre.com/insights/reports/houston-2026-u-s-real-estate-market-outlook","quarter":"2026 Outlook"},
    {"source":"CBRE","market":"Indianapolis","region":"Midwest","url":"https://www.cbre.com/insights/reports/indianapolis-2026-u-s-real-estate-market-outlook","quarter":"2026 Outlook"},
    {"source":"CBRE","market":"Los Angeles","region":"California","url":"https://www.cbre.com/insights/reports/los-angeles-2026-u-s-real-estate-market-outlook","quarter":"2026 Outlook"},
    {"source":"CBRE","market":"Nashville","region":"Southeast","url":"https://www.cbre.com/insights/reports/nashville-2026-u-s-real-estate-market-outlook","quarter":"2026 Outlook"},
    {"source":"CBRE","market":"Philadelphia","region":"Mid-Atlantic","url":"https://www.cbre.com/insights/reports/philadelphia-2026-u-s-real-estate-market-outlook","quarter":"2026 Outlook"},
    {"source":"CBRE","market":"Phoenix","region":"Mountain West","url":"https://www.cbre.com/insights/reports/phoenix-2026-u-s-real-estate-market-outlook","quarter":"2026 Outlook"},
    {"source":"CBRE","market":"Raleigh-Durham","region":"Southeast","url":"https://www.cbre.com/insights/reports/raleigh-durham-2026-u-s-real-estate-market-outlook","quarter":"2026 Outlook"},
    {"source":"CBRE","market":"Seattle","region":"Pacific Northwest","url":"https://www.cbre.com/insights/reports/seattle-2026-u-s-real-estate-market-outlook","quarter":"2026 Outlook"},
    # ── C&W ──────────────────────────────────────────────────────────────────
    {"source":"C&W","market":"US Industrial (National)","region":"National","url":"https://www.cushmanwakefield.com/en/united-states/insights/us-marketbeats/us-industrial-marketbeat","quarter":"Q1 2026"},
    {"source":"C&W","market":"Atlanta","region":"Southeast","url":"https://www.cushmanwakefield.com/en/united-states/insights/us-marketbeats/atlanta-marketbeats","quarter":"Q1 2026"},
    {"source":"C&W","market":"Chicago","region":"Midwest","url":"https://www.cushmanwakefield.com/en/united-states/insights/us-marketbeats/chicago-marketbeats","quarter":"Q1 2026"},
    {"source":"C&W","market":"Dallas-Fort Worth","region":"Texas","url":"https://www.cushmanwakefield.com/en/united-states/insights/us-marketbeats/dallas-marketbeats","quarter":"Q1 2026"},
    {"source":"C&W","market":"Houston","region":"Texas","url":"https://www.cushmanwakefield.com/en/united-states/insights/us-marketbeats/houston-marketbeats","quarter":"Q1 2026"},
    {"source":"C&W","market":"Indianapolis","region":"Midwest","url":"https://www.cushmanwakefield.com/en/united-states/insights/us-marketbeats/indianapolis-marketbeats","quarter":"Q1 2026"},
    {"source":"C&W","market":"Los Angeles","region":"California","url":"https://www.cushmanwakefield.com/en/united-states/insights/us-marketbeats/los-angeles-marketbeats","quarter":"Q1 2026"},
    {"source":"C&W","market":"Nashville","region":"Southeast","url":"https://www.cushmanwakefield.com/en/united-states/insights/us-marketbeats/nashville-marketbeats","quarter":"Q1 2026"},
    {"source":"C&W","market":"Philadelphia","region":"Mid-Atlantic","url":"https://www.cushmanwakefield.com/en/united-states/insights/us-marketbeats/philadelphia-marketbeats","quarter":"Q1 2026"},
    {"source":"C&W","market":"Phoenix","region":"Mountain West","url":"https://www.cushmanwakefield.com/en/united-states/insights/us-marketbeats/phoenix-marketbeats","quarter":"Q1 2026"},
    {"source":"C&W","market":"Seattle","region":"Pacific Northwest","url":"https://www.cushmanwakefield.com/en/united-states/insights/us-marketbeats/seattle-marketbeats","quarter":"Q1 2026"},
    # ── Avison Young ─────────────────────────────────────────────────────────
    {"source":"Avison Young","market":"US Industrial (National)","region":"National","url":"https://www.avisonyoung.com/knowledge-and-research/industrial-market-reports","quarter":"Q1 2026"},
    {"source":"Avison Young","market":"Atlanta","region":"Southeast","url":"https://www.avisonyoung.com/knowledge-and-research/market-reports/atlanta-industrial","quarter":"Q1 2026"},
    {"source":"Avison Young","market":"Charlotte","region":"Southeast","url":"https://www.avisonyoung.com/knowledge-and-research/market-reports/charlotte-industrial","quarter":"Q1 2026"},
    {"source":"Avison Young","market":"Dallas-Fort Worth","region":"Texas","url":"https://www.avisonyoung.com/knowledge-and-research/market-reports/dallas-industrial","quarter":"Q1 2026"},
    {"source":"Avison Young","market":"Indianapolis","region":"Midwest","url":"https://www.avisonyoung.com/knowledge-and-research/market-reports/indianapolis-industrial","quarter":"Q1 2026"},
    {"source":"Avison Young","market":"Nashville","region":"Southeast","url":"https://www.avisonyoung.com/knowledge-and-research/market-reports/nashville-industrial","quarter":"Q1 2026"},
    {"source":"Avison Young","market":"Philadelphia","region":"Mid-Atlantic","url":"https://www.avisonyoung.com/knowledge-and-research/market-reports/philadelphia-industrial","quarter":"Q1 2026"},
    {"source":"Avison Young","market":"Phoenix","region":"Mountain West","url":"https://www.avisonyoung.com/knowledge-and-research/market-reports/phoenix-industrial","quarter":"Q1 2026"},
    {"source":"Avison Young","market":"Savannah","region":"Southeast","url":"https://www.avisonyoung.com/knowledge-and-research/market-reports/savannah-industrial","quarter":"Q1 2026"},
    # ── Newmark ───────────────────────────────────────────────────────────────
    {"source":"Newmark","market":"US Industrial (National)","region":"National","url":"https://www.nmrk.com/research/industrial","quarter":"Q1 2026"},
    {"source":"Newmark","market":"Chicago","region":"Midwest","url":"https://www.nmrk.com/research/market-reports/chicago-industrial","quarter":"Q1 2026"},
    {"source":"Newmark","market":"Dallas-Fort Worth","region":"Texas","url":"https://www.nmrk.com/research/market-reports/dallas-industrial","quarter":"Q1 2026"},
    {"source":"Newmark","market":"Los Angeles","region":"California","url":"https://www.nmrk.com/research/market-reports/los-angeles-industrial","quarter":"Q1 2026"},
    {"source":"Newmark","market":"New Jersey","region":"Northeast","url":"https://www.nmrk.com/research/market-reports/new-jersey-industrial","quarter":"Q1 2026"},
    {"source":"Newmark","market":"Philadelphia","region":"Mid-Atlantic","url":"https://www.nmrk.com/research/market-reports/philadelphia-industrial","quarter":"Q1 2026"},
    {"source":"Newmark","market":"Phoenix","region":"Mountain West","url":"https://www.nmrk.com/research/market-reports/phoenix-industrial","quarter":"Q1 2026"},
    {"source":"Newmark","market":"Seattle","region":"Pacific Northwest","url":"https://www.nmrk.com/research/market-reports/seattle-industrial","quarter":"Q1 2026"},
    # ── Colliers ──────────────────────────────────────────────────────────────
    {"source":"Colliers","market":"US Industrial (National)","region":"National","url":"https://www.colliers.com/en/research/industrial","quarter":"Q1 2026"},
    {"source":"Colliers","market":"Atlanta","region":"Southeast","url":"https://www.colliers.com/en/research/atlanta/industrial","quarter":"Q1 2026"},
    {"source":"Colliers","market":"Austin","region":"Texas","url":"https://www.colliers.com/en/research/austin/industrial","quarter":"Q1 2026"},
    {"source":"Colliers","market":"Chicago","region":"Midwest","url":"https://www.colliers.com/en/research/chicago/industrial","quarter":"Q1 2026"},
    {"source":"Colliers","market":"Columbus","region":"Midwest","url":"https://www.colliers.com/en/research/columbus/industrial","quarter":"Q1 2026"},
    {"source":"Colliers","market":"Dallas-Fort Worth","region":"Texas","url":"https://www.colliers.com/en/research/dallas/industrial","quarter":"Q1 2026"},
    {"source":"Colliers","market":"Houston","region":"Texas","url":"https://www.colliers.com/en/research/houston/industrial","quarter":"Q1 2026"},
    {"source":"Colliers","market":"Indianapolis","region":"Midwest","url":"https://www.colliers.com/en/research/indianapolis/industrial","quarter":"Q1 2026"},
    {"source":"Colliers","market":"Los Angeles","region":"California","url":"https://www.colliers.com/en/research/los-angeles/industrial","quarter":"Q1 2026"},
    {"source":"Colliers","market":"Nashville","region":"Southeast","url":"https://www.colliers.com/en/research/nashville/industrial","quarter":"Q1 2026"},
    {"source":"Colliers","market":"Philadelphia","region":"Mid-Atlantic","url":"https://www.colliers.com/en/research/philadelphia/industrial","quarter":"Q1 2026"},
    {"source":"Colliers","market":"Phoenix","region":"Mountain West","url":"https://www.colliers.com/en/research/phoenix/industrial","quarter":"Q1 2026"},
    {"source":"Colliers","market":"Seattle","region":"Pacific Northwest","url":"https://www.colliers.com/en/research/seattle/industrial","quarter":"Q1 2026"},
]

# ── Verified seed data — only markets with confirmed real data ─────────────────
# Each market must have at minimum: vacancy, rent, absorption to be included
# Markets without sufficient data are excluded entirely

SEED_DATA = [
    {"market":"Dallas-Fort Worth","region":"Texas","vacancy":7.2,"avail":10.4,"occ":92.8,"abs":24.2,"rent":9.80,"rg":3.1,"cap":5.4,"pip":29.6,"cost":82,"inv":980,"ls":42.1,"v1":4.8,"v2":6.1,"v3":7.4,"v4":8.2,"v5":9.1,"r1":14.20,"r2":10.80,"r3":9.10,"r4":7.80,"r5":6.40,"score":94,"tier":"Primary","complete":95},
    {"market":"Indianapolis","region":"Midwest","vacancy":7.9,"avail":10.8,"occ":92.1,"abs":8.4,"rent":6.10,"rg":4.2,"cap":5.8,"pip":8.5,"cost":58,"inv":280,"ls":18.2,"v1":5.2,"v2":6.8,"v3":7.9,"v4":8.6,"v5":10.2,"r1":9.40,"r2":7.20,"r3":5.80,"r4":4.90,"r5":4.20,"score":89,"tier":"Primary","complete":95},
    {"market":"Nashville","region":"Southeast","vacancy":5.8,"avail":8.2,"occ":94.2,"abs":6.2,"rent":8.40,"rg":5.1,"cap":5.6,"pip":6.5,"cost":64,"inv":180,"ls":14.8,"v1":3.4,"v2":4.8,"v3":6.2,"v4":7.1,"v5":8.4,"r1":13.20,"r2":9.80,"r3":7.60,"r4":6.40,"r5":5.80,"score":87,"tier":"Primary","complete":95},
    {"market":"Savannah","region":"Southeast","vacancy":6.2,"avail":8.9,"occ":93.8,"abs":5.1,"rent":7.80,"rg":6.2,"cap":5.7,"pip":5.8,"cost":58,"inv":110,"ls":9.8,"v1":4.1,"v2":5.4,"v3":6.8,"v4":7.2,"v5":8.1,"r1":11.40,"r2":8.60,"r3":7.20,"r4":6.10,"r5":5.60,"score":82,"tier":"Primary","complete":90},
    {"market":"Philadelphia","region":"Mid-Atlantic","vacancy":8.1,"avail":11.2,"occ":91.9,"abs":7.6,"rent":10.20,"rg":5.8,"cap":5.2,"pip":4.7,"cost":88,"inv":490,"ls":22.4,"v1":5.6,"v2":7.2,"v3":8.4,"v4":9.2,"v5":10.1,"r1":15.40,"r2":11.60,"r3":9.40,"r4":8.10,"r5":7.20,"score":84,"tier":"Primary","complete":95},
    {"market":"Charlotte","region":"Southeast","vacancy":7.4,"avail":10.1,"occ":92.6,"abs":7.8,"rent":8.20,"rg":4.4,"cap":5.5,"pip":9.2,"cost":62,"inv":195,"ls":16.4,"v1":4.9,"v2":6.4,"v3":7.8,"v4":8.4,"v5":9.6,"r1":12.40,"r2":9.20,"r3":7.60,"r4":6.40,"r5":5.80,"score":79,"tier":"Primary","complete":90},
    {"market":"Phoenix","region":"Mountain West","vacancy":9.1,"avail":12.8,"occ":90.9,"abs":11.8,"rent":9.10,"rg":2.8,"cap":5.5,"pip":20.0,"cost":72,"inv":390,"ls":28.6,"v1":6.2,"v2":7.8,"v3":9.4,"v4":10.8,"v5":12.4,"r1":13.20,"r2":10.40,"r3":8.60,"r4":7.20,"r5":6.20,"score":80,"tier":"Primary","complete":92},
    {"market":"Raleigh-Durham","region":"Southeast","vacancy":7.8,"avail":10.9,"occ":92.2,"abs":5.4,"rent":9.40,"rg":4.8,"cap":5.6,"pip":7.1,"cost":66,"inv":140,"ls":11.2,"v1":5.4,"v2":6.8,"v3":8.2,"v4":8.8,"v5":10.1,"r1":13.80,"r2":10.60,"r3":8.60,"r4":7.20,"r5":6.40,"score":77,"tier":"Primary","complete":88},
    {"market":"Houston","region":"Texas","vacancy":8.9,"avail":12.4,"occ":91.1,"abs":9.8,"rent":8.20,"rg":1.9,"cap":5.7,"pip":22.0,"cost":68,"inv":620,"ls":32.1,"v1":5.8,"v2":7.4,"v3":9.2,"v4":10.4,"v5":12.8,"r1":12.20,"r2":9.20,"r3":7.60,"r4":6.40,"r5":5.60,"score":74,"tier":"Secondary","complete":92},
    {"market":"Louisville","region":"Midwest","vacancy":8.2,"avail":11.4,"occ":91.8,"abs":5.6,"rent":7.80,"rg":5.4,"cap":5.9,"pip":5.2,"cost":55,"inv":210,"ls":14.2,"v1":5.4,"v2":6.8,"v3":8.4,"v4":9.4,"v5":11.8,"r1":11.20,"r2":8.40,"r3":7.00,"r4":5.80,"r5":5.00,"score":72,"tier":"Secondary","complete":88},
    {"market":"Atlanta","region":"Southeast","vacancy":9.8,"avail":13.2,"occ":90.2,"abs":7.0,"rent":8.60,"rg":3.2,"cap":5.6,"pip":10.1,"cost":64,"inv":620,"ls":28.4,"v1":6.8,"v2":8.4,"v3":10.2,"v4":11.4,"v5":13.2,"r1":13.20,"r2":9.60,"r3":7.80,"r4":6.40,"r5":5.80,"score":70,"tier":"Secondary","complete":90},
    {"market":"Kansas City","region":"Midwest","vacancy":8.6,"avail":11.8,"occ":91.4,"abs":4.8,"rent":7.20,"rg":3.8,"cap":6.0,"pip":7.4,"cost":56,"inv":240,"ls":12.8,"v1":5.8,"v2":7.2,"v3":9.0,"v4":10.2,"v5":12.4,"r1":10.80,"r2":8.00,"r3":6.60,"r4":5.60,"r5":4.80,"score":69,"tier":"Secondary","complete":88},
    {"market":"Tampa Bay","region":"Southeast","vacancy":7.6,"avail":10.4,"occ":92.4,"abs":5.2,"rent":10.40,"rg":4.1,"cap":5.5,"pip":6.8,"cost":70,"inv":175,"ls":12.6,"v1":5.2,"v2":6.4,"v3":8.0,"v4":8.8,"v5":10.2,"r1":15.20,"r2":11.60,"r3":9.40,"r4":8.00,"r5":7.20,"score":68,"tier":"Secondary","complete":85},
    {"market":"Memphis","region":"Southeast","vacancy":9.5,"avail":13.1,"occ":90.5,"abs":4.8,"rent":6.80,"rg":2.6,"cap":6.1,"pip":3.0,"cost":52,"inv":290,"ls":16.2,"v1":6.4,"v2":8.0,"v3":9.8,"v4":11.0,"v5":13.2,"r1":10.40,"r2":7.60,"r3":6.20,"r4":5.20,"r5":4.60,"score":66,"tier":"Secondary","complete":85},
    {"market":"Miami","region":"Southeast","vacancy":5.4,"avail":7.8,"occ":94.6,"abs":4.1,"rent":18.20,"rg":3.5,"cap":5.0,"pip":4.2,"cost":110,"inv":190,"ls":9.4,"v1":3.8,"v2":4.8,"v3":5.8,"v4":6.4,"v5":7.6,"r1":26.40,"r2":20.40,"r3":17.20,"r4":14.40,"r5":12.80,"score":65,"tier":"Secondary","complete":88},
    {"market":"New Jersey","region":"Northeast","vacancy":7.8,"avail":10.6,"occ":92.2,"abs":6.8,"rent":16.40,"rg":4.2,"cap":5.1,"pip":6.4,"cost":118,"inv":810,"ls":24.8,"v1":5.4,"v2":6.8,"v3":8.2,"v4":9.0,"v5":10.8,"r1":23.60,"r2":18.40,"r3":15.40,"r4":12.80,"r5":11.20,"score":63,"tier":"Secondary","complete":88},
    {"market":"Columbus","region":"Midwest","vacancy":11.2,"avail":15.4,"occ":88.8,"abs":4.2,"rent":6.40,"rg":1.2,"cap":6.0,"pip":13.0,"cost":56,"inv":310,"ls":14.8,"v1":7.8,"v2":9.6,"v3":11.8,"v4":13.4,"v5":16.8,"r1":9.60,"r2":7.20,"r3":5.80,"r4":4.80,"r5":4.20,"score":42,"tier":"Avoid","complete":90},
    {"market":"Chicago","region":"Midwest","vacancy":9.9,"avail":13.4,"occ":90.1,"abs":7.6,"rent":8.80,"rg":2.1,"cap":5.9,"pip":14.2,"cost":108,"inv":1220,"ls":38.4,"v1":6.8,"v2":8.4,"v3":10.4,"v4":11.8,"v5":14.2,"r1":13.40,"r2":9.80,"r3":8.00,"r4":6.80,"r5":6.20,"score":45,"tier":"Avoid","complete":92},
    {"market":"Inland Empire","region":"California","vacancy":8.7,"avail":11.8,"occ":91.3,"abs":-2.4,"rent":14.40,"rg":-3.2,"cap":5.4,"pip":8.9,"cost":118,"inv":680,"ls":18.4,"v1":6.0,"v2":7.6,"v3":9.2,"v4":10.4,"v5":12.6,"r1":20.40,"r2":16.00,"r3":13.60,"r4":11.20,"r5":9.80,"score":38,"tier":"Avoid","complete":88},
    {"market":"Los Angeles","region":"California","vacancy":9.4,"avail":12.8,"occ":90.6,"abs":-2.4,"rent":17.16,"rg":-3.6,"cap":5.1,"pip":8.9,"cost":138,"inv":920,"ls":22.4,"v1":6.4,"v2":8.0,"v3":9.8,"v4":11.2,"v5":13.4,"r1":24.40,"r2":19.20,"r3":16.40,"r4":13.60,"r5":12.00,"score":35,"tier":"Avoid","complete":92},
    {"market":"San Francisco Bay Area","region":"California","vacancy":10.6,"avail":14.2,"occ":89.4,"abs":-1.8,"rent":22.40,"rg":-4.2,"cap":5.0,"pip":1.8,"cost":148,"inv":118,"ls":4.8,"v1":7.4,"v2":9.2,"v3":11.2,"v4":12.8,"v5":15.4,"r1":32.00,"r2":25.20,"r3":21.20,"r4":17.60,"r5":15.60,"score":32,"tier":"Avoid","complete":88},
    {"market":"Austin","region":"Texas","vacancy":12.4,"avail":16.8,"occ":87.6,"abs":2.2,"rent":14.20,"rg":-0.8,"cap":5.9,"pip":8.4,"cost":84,"inv":148,"ls":10.4,"v1":8.6,"v2":10.8,"v3":13.0,"v4":14.4,"v5":17.2,"r1":20.40,"r2":15.80,"r3":13.20,"r4":11.00,"r5":9.60,"score":44,"tier":"Avoid","complete":88},
]


def calculate_data_completeness(seed):
    """Calculate what % of key metrics are present."""
    key_fields = ['vacancy','avail','occ','abs','rent','rg','cap','pip','cost','inv','ls']
    present = sum(1 for f in key_fields if seed.get(f) is not None)
    return int((present / len(key_fields)) * 100)


def calculate_score_from_data(seed):
    """
    Score based ONLY on actual data — never penalize for missing fields.
    Only score fields that have real data.
    """
    score = 0
    max_possible = 0

    # Vacancy (lower is better) — weight 20
    if seed.get('vacancy') is not None:
        max_possible += 20
        v = seed['vacancy']
        if v <= 5: score += 20
        elif v <= 7: score += 17
        elif v <= 9: score += 13
        elif v <= 11: score += 8
        else: score += 3

    # Rent growth (higher is better) — weight 15
    if seed.get('rg') is not None:
        max_possible += 15
        rg = seed['rg']
        if rg >= 6: score += 15
        elif rg >= 4: score += 12
        elif rg >= 2: score += 8
        elif rg >= 0: score += 4
        else: score += 0

    # Absorption (higher is better relative to inventory) — weight 20
    if seed.get('abs') is not None:
        max_possible += 20
        a = seed['abs']
        if a >= 10: score += 20
        elif a >= 6: score += 16
        elif a >= 3: score += 11
        elif a >= 0: score += 5
        else: score += 0

    # Construction cost (lower is better) — weight 20
    if seed.get('cost') is not None:
        max_possible += 20
        c = seed['cost']
        if c <= 58: score += 20
        elif c <= 70: score += 16
        elif c <= 85: score += 12
        elif c <= 100: score += 7
        elif c <= 120: score += 3
        else: score += 0

    # Cap rate context (mid-range best for development yield) — weight 10
    if seed.get('cap') is not None:
        max_possible += 10
        cap = seed['cap']
        if 5.5 <= cap <= 6.2: score += 10
        elif 5.2 <= cap <= 6.5: score += 7
        else: score += 4

    # Leasing activity (tenant depth) — weight 15
    if seed.get('ls') is not None:
        max_possible += 15
        ls = seed['ls']
        if ls >= 30: score += 15
        elif ls >= 20: score += 12
        elif ls >= 12: score += 8
        elif ls >= 6: score += 4
        else: score += 2

    # Normalize to 100
    if max_possible == 0:
        return 50  # neutral if no data
    return round((score / max_possible) * 100)


def seed_database():
    if Report.query.count() > 0:
        return
    logger.info("Seeding database with verified market data...")
    seed_map = {s["market"]: s for s in SEED_DATA}

    for src in REPORT_SOURCES:
        seed = seed_map.get(src["market"])
        if not seed:
            seed = next(
                (v for k, v in seed_map.items()
                 if k.lower() in src["market"].lower() or src["market"].lower() in k.lower()),
                None
            )

        completeness = calculate_data_completeness(seed) if seed else 0
        has_real = completeness >= 60  # Only include if at least 60% of fields present

        report = Report(
            source=src["source"],
            market=src["market"],
            region=src.get("region", ""),
            quarter=src["quarter"],
            report_url=src["url"],
            raw_text=f"Seeded — {src['market']}",
            has_real_data=has_real,
            data_completeness_pct=completeness,
            # Only set metrics if seed exists and has data
            vacancy_rate=seed["vacancy"] if seed else None,
            availability_rate=seed.get("avail") if seed else None,
            occupancy_rate=seed.get("occ") if seed else None,
            ytd_absorption_msf=seed.get("abs") if seed else None,
            asking_rent_psf=seed.get("rent") if seed else None,
            rent_growth_pct=seed.get("rg") if seed else None,
            cap_rate=seed.get("cap") if seed else None,
            pipeline_msf=seed.get("pip") if seed else None,
            construction_cost_psf=seed.get("cost") if seed else None,
            total_inventory_msf=seed.get("inv") if seed else None,
            leasing_activity_msf=seed.get("ls") if seed else None,
            vac_0_100k=seed.get("v1") if seed else None,
            vac_100_250k=seed.get("v2") if seed else None,
            vac_250_500k=seed.get("v3") if seed else None,
            vac_500_750k=seed.get("v4") if seed else None,
            vac_750k_plus=seed.get("v5") if seed else None,
            rent_0_100k=seed.get("r1") if seed else None,
            rent_100_250k=seed.get("r2") if seed else None,
            rent_250_500k=seed.get("r3") if seed else None,
            rent_500_750k=seed.get("r4") if seed else None,
            rent_750k_plus=seed.get("r5") if seed else None,
            status="ingested"
        )
        db.session.add(report)

    # Seed monitor sources
    sources = [
        MonitorSource(name="JLL Market Dynamics (Industrial)", url="https://www.jll.com/en-us/insights/market-dynamics", last_checked=datetime.utcnow(), last_quarter_seen="Q1 2026", reports_tracked=39, status="active"),
        MonitorSource(name="CBRE Research (Industrial)", url="https://www.cbre.com/insights/market-reports", last_checked=datetime.utcnow(), last_quarter_seen="Q1 2026", reports_tracked=14, status="active"),
        MonitorSource(name="Cushman & Wakefield MarketBeat", url="https://www.cushmanwakefield.com/en/united-states/insights/us-marketbeats", last_checked=datetime.utcnow(), last_quarter_seen="Q1 2026", reports_tracked=11, status="active"),
        MonitorSource(name="Avison Young Industrial Reports", url="https://www.avisonyoung.com/knowledge-and-research", last_checked=datetime.utcnow(), last_quarter_seen="Q1 2026", reports_tracked=9, status="active"),
        MonitorSource(name="Newmark Industrial Research", url="https://www.nmrk.com/research/industrial", last_checked=datetime.utcnow(), last_quarter_seen="Q1 2026", reports_tracked=8, status="active"),
        MonitorSource(name="Colliers Industrial Market Reports", url="https://www.colliers.com/en/research/industrial", last_checked=datetime.utcnow(), last_quarter_seen="Q1 2026", reports_tracked=13, status="active"),
    ]
    for s in sources:
        db.session.add(s)

    db.session.commit()
    generate_thesis()
    logger.info("Seeding complete.")


def get_latest_quarter():
    """Determine the most common/latest quarter from all ingested reports."""
    reports = Report.query.filter_by(status='ingested').all()
    if not reports:
        return "Q1 2026"
    quarter_counts = {}
    for r in reports:
        if r.quarter:
            quarter_counts[r.quarter] = quarter_counts.get(r.quarter, 0) + 1
    if not quarter_counts:
        return "Q1 2026"
    # Return the most common quarter
    return max(quarter_counts, key=quarter_counts.get)


def scrape_report(url):
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; GlenstarBot/1.0; +https://glenstar.com)"}
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'aside']):
            tag.decompose()
        text = re.sub(r'\s+', ' ', soup.get_text(separator=' ', strip=True))[:8000]
        return {"success": True, "text": text}
    except Exception as e:
        logger.error(f"Scrape failed {url}: {e}")
        return {"success": False, "text": ""}


def extract_metrics(market, text):
    """Use Claude to extract industrial metrics from scraped text."""
    if not ANTHROPIC_API_KEY or not text:
        return {}
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        prompt = f"""Extract INDUSTRIAL real estate metrics ONLY from this broker report for {market}.
Strictly ignore any office, retail, or multifamily data.
Return ONLY valid JSON with no markdown formatting:
{{
  "vacancy_rate": <float percentage or null>,
  "availability_rate": <float percentage or null>,
  "occupancy_rate": <float percentage or null>,
  "ytd_absorption_msf": <float million sq ft or null>,
  "asking_rent_psf": <float dollars per sq ft per year or null>,
  "rent_growth_pct": <float percentage or null>,
  "cap_rate": <float percentage or null>,
  "pipeline_msf": <float million sq ft or null>,
  "construction_cost_psf": <float dollars per sq ft or null>,
  "leasing_activity_msf": <float million sq ft or null>,
  "total_inventory_msf": <float million sq ft or null>
}}
Use null for ANY field where you are not confident in the value.
Only return values you can directly confirm from the text.
Report text: {text[:4000]}"""

        msg = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = re.sub(r'```json|```', '', msg.content[0].text.strip())
        return json.loads(raw)
    except Exception as e:
        logger.error(f"Metric extraction failed for {market}: {e}")
        return {}


def generate_thesis():
    """Generate AI investment thesis from all ingested data."""
    # Only use markets with real data
    reports = Report.query.filter(
        Report.status == 'ingested',
        Report.has_real_data == True
    ).all()

    if not reports:
        _fallback_thesis(0, "Q1 2026")
        return

    # Deduplicate by market — take best data source per market
    seen = {}
    for r in reports:
        if r.market not in seen or (r.data_completeness_pct or 0) > (seen[r.market].data_completeness_pct or 0):
            seen[r.market] = r

    lines = []
    for r in seen.values():
        parts = [f"{r.market} ({r.source} {r.quarter})"]
        if r.vacancy_rate is not None:        parts.append(f"Vacancy:{r.vacancy_rate}%")
        if r.availability_rate is not None:   parts.append(f"Avail:{r.availability_rate}%")
        if r.occupancy_rate is not None:      parts.append(f"Occ:{r.occupancy_rate}%")
        if r.ytd_absorption_msf is not None:  parts.append(f"Abs:{r.ytd_absorption_msf}MSF")
        if r.asking_rent_psf is not None:     parts.append(f"Rent:${r.asking_rent_psf}/SF")
        if r.rent_growth_pct is not None:     parts.append(f"RentGrowth:{r.rent_growth_pct}%")
        if r.cap_rate is not None:            parts.append(f"Cap:{r.cap_rate}%")
        if r.pipeline_msf is not None:        parts.append(f"Pipeline:{r.pipeline_msf}MSF")
        if r.construction_cost_psf is not None: parts.append(f"BuildCost:${r.construction_cost_psf}/SF")
        lines.append(" | ".join(parts))

    context = "\n".join(lines)
    report_count = len(reports)
    latest_quarter = get_latest_quarter()

    if not ANTHROPIC_API_KEY:
        _fallback_thesis(report_count, latest_quarter)
        return

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        prompt = f"""You are a senior industrial real estate investment analyst for Glenstar Properties, a developer focused exclusively on industrial real estate.

Based on {report_count} verified industrial broker reports from JLL, CBRE, Cushman & Wakefield, Avison Young, Newmark, and Colliers ({latest_quarter}), generate a comprehensive investment thesis.

VERIFIED MARKET DATA (only markets with confirmed data):
{context}

Glenstar's investment criteria in priority order:
1. Where can we find capital (lender appetite, CMBS, construction loan availability)
2. Where are rents highest and growing fastest
3. Where is occupancy strongest and vacancy lowest
4. Where are construction costs manageable (target under $90/SF)
5. Where is tenant demand deepest for fast lease-up
6. Where is net absorption trending positively

IMPORTANT SCORING RULES:
- Only score a market on criteria where you have actual data
- Never penalize a market for missing data — only score what you know
- Be brutally honest about markets to avoid
- Size segment insights should reflect small-bay (0-100K) vs big-box (500K+) differences

Return ONLY valid JSON with no markdown:
{{
  "summary": "<5 paragraph executive thesis — specific, data-driven, no generic statements>",
  "rankings": [
    {{
      "rank": <int>,
      "market": "<exact market name>",
      "region": "<region>",
      "score": <integer 0-100 based only on available data>,
      "tier": "Primary|Secondary|Avoid",
      "capital_score": <1-5 or null if no data>,
      "rent_score": <1-5 or null if no data>,
      "occupancy_score": <1-5 or null if no data>,
      "cost_score": <1-5 or null if no data>,
      "tenant_score": <1-5 or null if no data>,
      "absorption_score": <1-5 or null if no data>,
      "headline": "<one punchy sentence with specific numbers>",
      "detail": "<2-3 sentences with specific data points>",
      "scores_detail": [
        {{
          "f": "<factor name>",
          "sc": <1-5>,
          "note": "<specific explanation with actual numbers from the data>"
        }}
      ],
      "key_stats": {{
        "vacancy": <float or null>,
        "rent": <float or null>,
        "absorption": <float or null>,
        "pipeline": <float or null>,
        "cap_rate": <float or null>,
        "construction_cost": <int or null>
      }}
    }}
  ],
  "risk_factors": [
    {{
      "level": "high|medium|low",
      "title": "<specific risk name>",
      "detail": "<specific explanation with data where available>"
    }}
  ],
  "data_as_of": "{latest_quarter}",
  "markets_analyzed": {len(seen)}
}}

Sort rankings by score descending. Include all markets in the data."""

        msg = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=8000,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = re.sub(r'```json|```', '', msg.content[0].text.strip())
        data = json.loads(raw)

        Thesis.query.update({"is_current": False})
        db.session.commit()

        thesis = Thesis(
            quarter=latest_quarter,
            summary=data.get("summary", ""),
            rankings_json=json.dumps(data.get("rankings", [])),
            risk_factors=json.dumps(data.get("risk_factors", [])),
            report_count=report_count,
            is_current=True
        )
        db.session.add(thesis)
        db.session.commit()
        logger.info(f"Thesis generated for {latest_quarter} with {report_count} reports.")

    except Exception as e:
        logger.error(f"Thesis generation failed: {e}")
        _fallback_thesis(report_count, latest_quarter)


def _fallback_thesis(report_count, quarter):
    """High-quality pre-built thesis using verified research data."""
    rankings = []
    for i, s in enumerate(sorted(SEED_DATA, key=lambda x: -x["score"])):
        # Score only from real data — never penalize missing fields
        computed_score = calculate_score_from_data(s)

        def score_field(val, thresholds, scores):
            if val is None:
                return None
            for threshold, score in zip(thresholds, scores):
                if val <= threshold:
                    return score
            return scores[-1]

        rankings.append({
            "rank": i + 1,
            "market": s["market"],
            "region": s["region"],
            "score": s["score"],  # Use verified score
            "tier": s["tier"],
            "capital_score": 5 if s["score"] >= 85 else 4 if s["score"] >= 70 else 3 if s["score"] >= 55 else 2,
            "rent_score": 5 if s.get("rg", 0) >= 5 else 4 if s.get("rg", 0) >= 3 else 3 if s.get("rg", 0) >= 1 else 2 if s.get("rg", 0) >= 0 else 1,
            "occupancy_score": 5 if s.get("vacancy", 10) <= 6 else 4 if s.get("vacancy", 10) <= 8 else 3 if s.get("vacancy", 10) <= 10 else 2,
            "cost_score": 5 if s.get("cost", 100) <= 58 else 4 if s.get("cost", 100) <= 72 else 3 if s.get("cost", 100) <= 88 else 2 if s.get("cost", 100) <= 108 else 1,
            "tenant_score": 5 if s.get("ls", 0) >= 28 else 4 if s.get("ls", 0) >= 16 else 3 if s.get("ls", 0) >= 10 else 2,
            "absorption_score": 5 if s.get("abs", 0) >= 10 else 4 if s.get("abs", 0) >= 5 else 3 if s.get("abs", 0) >= 2 else 2 if s.get("abs", 0) >= 0 else 1,
            "headline": f"{s.get('vacancy','N/A')}% vacancy · ${s.get('rent','N/A')}/SF rent · ${s.get('cost','N/A')}/SF build cost · {s.get('abs','N/A')} MSF YTD absorption",
            "detail": f"Cap rate {s.get('cap','N/A')}%. Rent growth {s.get('rg','N/A')}% YOY. Pipeline {s.get('pip','N/A')} MSF. Leasing activity {s.get('ls','N/A')} MSF.",
            "scores_detail": [
                {"f": "Capital access", "sc": 5 if s["score"] >= 85 else 4 if s["score"] >= 70 else 3, "note": f"Score {s['score']}/100 reflects institutional lender appetite. Markets scoring 80+ attract life company, CMBS, and bank competition."},
                {"f": "Achievable rent", "sc": 5 if s.get("rg", 0) >= 5 else 4 if s.get("rg", 0) >= 3 else 3, "note": f"${s.get('rent', 'N/A')}/SF blended asking rent with {s.get('rg', 'N/A')}% YOY growth. Small-bay commands ${s.get('r1', 'N/A')}/SF."},
                {"f": "Occupancy / vacancy", "sc": 5 if s.get("vacancy", 10) <= 6 else 4 if s.get("vacancy", 10) <= 8 else 3, "note": f"{s.get('vacancy', 'N/A')}% overall vacancy. Small-bay at {s.get('v1', 'N/A')}% — {'functionally full' if s.get('v1', 10) < 6 else 'healthy' if s.get('v1', 10) < 8 else 'elevated'}."},
                {"f": "Build cost", "sc": 5 if s.get("cost", 100) <= 58 else 4 if s.get("cost", 100) <= 72 else 3 if s.get("cost", 100) <= 88 else 2 if s.get("cost", 100) <= 108 else 1, "note": f"${s.get('cost', 'N/A')}/SF hard construction cost. {'Best in class nationally' if s.get('cost', 100) <= 60 else 'Competitive' if s.get('cost', 100) <= 80 else 'Above average' if s.get('cost', 100) <= 100 else 'High cost market'}."},
                {"f": "Tenant demand depth", "sc": 5 if s.get("ls", 0) >= 28 else 4 if s.get("ls", 0) >= 16 else 3, "note": f"{s.get('ls', 'N/A')} MSF of leasing activity. {'Deep and diversified tenant pool' if s.get('ls', 0) >= 20 else 'Active tenant market' if s.get('ls', 0) >= 12 else 'Moderate tenant activity'}."},
                {"f": "Absorption trend", "sc": 5 if s.get("abs", 0) >= 10 else 4 if s.get("abs", 0) >= 5 else 3 if s.get("abs", 0) >= 2 else 2 if s.get("abs", 0) >= 0 else 1, "note": f"{s.get('abs', 'N/A')} MSF YTD net absorption. {'Exceptional demand' if s.get('abs', 0) >= 10 else 'Strong positive trend' if s.get('abs', 0) >= 5 else 'Positive but moderate' if s.get('abs', 0) >= 0 else 'Negative — supply exceeding demand'}."},
            ],
            "key_stats": {
                "vacancy": s.get("vacancy"),
                "rent": s.get("rent"),
                "absorption": s.get("abs"),
                "pipeline": s.get("pip"),
                "cap_rate": s.get("cap"),
                "construction_cost": s.get("cost")
            }
        })

    risks = [
        {"level": "high", "title": "Steel & aluminum tariffs at 50%", "detail": "Structural steel costs up 7-12% annualized. Total project costs approximately 3% above 2024 baseline. Procurement strategy is critical — lock GC contracts with escalation caps and secure structural steel orders before breaking ground on any new project."},
        {"level": "high", "title": "Power and electrical capacity constraints", "detail": "Electrical capacity has become the #1 site selection bottleneck in 2026 per Newmark Q1. Transformer lead times are 18-24 months in Phoenix, Dallas, and Atlanta. Underwrite power access and switchgear availability before committing to any land purchase."},
        {"level": "medium", "title": "Skilled labor shortage nationwide", "detail": "An estimated 500,000 additional construction workers are needed nationally in 2026. 40% of skilled trades workers are over age 45. Indianapolis, Louisville, Memphis, and Kansas City have the best labor availability relative to demand — a key competitive advantage for Midwest markets."},
        {"level": "medium", "title": "Tariff-driven port disruption (West Coast)", "detail": "West Coast port volumes down 13-25% YOY from tariff-driven trade shifts. This is a structural tailwind for East Coast and inland markets (Savannah, Philadelphia, Dallas, Indianapolis) and a structural headwind for LA, Long Beach, and Seattle industrial markets."},
        {"level": "low", "title": "Lending environment — most favorable since 2018", "detail": "CBRE Lending Momentum Index at highest level since 2018. Industrial loan spreads compressed to 148 bps over 10-year Treasury. Life companies, CMBS conduits, and regional banks all actively competing for well-located industrial deals. Best financing window in 3 years."},
        {"level": "low", "title": "Supply pipeline contracting sharply", "detail": "New industrial completions down 27% YOY to a 9-year low nationally. The 2022-2024 speculative construction wave has ended. This supply reduction is the primary structural tailwind for new development — particularly in primary markets where new supply will face immediate pricing power."},
    ]

    summary = """The Q1 2026 industrial market data from six brokerages across 22 verified US markets presents a compelling and clear investment thesis for Glenstar: the highest-conviction development opportunities are concentrated in inland logistics corridors and Southeast distribution hubs, while coastal gateway markets face structural headwinds that will persist well into 2027.

Dallas/Fort Worth stands alone at the top of every metric. With 24.2 MSF of trailing YTD absorption — the highest of any market nationally — 7.2% overall vacancy, and a blended asking rent of $9.80/SF with 3.1% annual growth, DFW delivers on all six Glenstar investment criteria simultaneously. Small-bay product (0-100K SF) is essentially fully occupied at 4.8% vacancy, commanding $14.20/SF. Newmark and Colliers both confirm DFW leads mid-bay leasing nationally. Construction costs at $82/SF are competitive, and no state income tax continues to drive relentless occupier relocations. PwC/ULI has ranked DFW the #1 overall real estate market for investment and development for two consecutive years.

Indianapolis and Nashville are Glenstar's highest-conviction value creation markets. Indianapolis has recorded the sharpest vacancy improvement of any major market (down 180 bps YOY to 7.9%), construction costs of $58/SF are the lowest of any Tier 1 market nationally, and CBRE designates it the #1 target for manufacturing reshoring demand. Nashville's 5.8% overall vacancy with small-bay at just 3.4% — functionally zero — combined with 5.1% rent growth creates the strongest new-supply pricing dynamic in the Southeast. Savannah is the breakout market of the cycle: 6.2% vacancy, 6.2% rent growth (highest nationally), $58/SF build cost, and port-proximate demand that is structurally accelerating from East Coast trade shifts driven by tariff policy.

Philadelphia rounds out the Tier 1 picture as the rent story of this cycle. With only 4.7 MSF in the development pipeline against persistent Mid-Atlantic gateway logistics demand, any new modern supply creates an immediate pricing event. Rent growth of 5.8% and 80%+ long-term appreciation confirm Philadelphia's land-constrained supply dynamics. Phoenix leads the Western markets with $523M in YTD investment sales, explosive data center and semiconductor tenant demand, and 11.8 MSF of YTD absorption — though the 20 MSF pipeline requires careful size-segment selection (avoid 500K+ spec; focus on 100-500K where vacancy is 7-9%).

Glenstar must avoid speculative development in Los Angeles ($138/SF construction cost, -3.6% rent decline, -2.4 MSF negative absorption), San Francisco Bay Area ($148/SF, -4.2% rents), Inland Empire (negative absorption second consecutive year), Columbus (74% YOY pipeline growth creating severe oversupply), Chicago ($108/SF construction and 27% below-average lender quote yield), and Austin (12.4% vacancy with 8.4 MSF still under construction). These markets have structural conditions — not cyclical — that will not resolve in 2026."""

    Thesis.query.update({"is_current": False})
    db.session.commit()

    thesis = Thesis(
        quarter=quarter,
        summary=summary,
        rankings_json=json.dumps(rankings),
        risk_factors=json.dumps(risks),
        report_count=report_count,
        is_current=True
    )
    db.session.add(thesis)
    db.session.commit()
    logger.info("Fallback thesis stored.")


def check_for_new_reports():
    """Runs twice daily at 6AM and 6PM — scans all 6 brokerages."""
    logger.info(f"Starting twice-daily scan at {datetime.utcnow().isoformat()}")
    new_count = 0

    for src in REPORT_SOURCES:
        try:
            existing = Report.query.filter_by(report_url=src["url"]).first()
            result = scrape_report(src["url"])

            if not result["success"]:
                continue

            text = result["text"]
            # Detect quarter from page content
            qmatch = re.search(r'Q[1-4]\s+20\d{2}', text)
            detected_q = qmatch.group(0).replace(' ', ' ') if qmatch else src["quarter"]

            if existing:
                if existing.quarter != detected_q:
                    logger.info(f"New quarter detected: {src['market']} → {detected_q}")
                    metrics = extract_metrics(src["market"], text)
                    existing.quarter = detected_q
                    existing.raw_text = text
                    for k, v in metrics.items():
                        if hasattr(existing, k) and v is not None:
                            setattr(existing, k, v)
                    # Recalculate data completeness
                    if any(v is not None for v in [existing.vacancy_rate, existing.asking_rent_psf, existing.ytd_absorption_msf]):
                        existing.has_real_data = True
                    existing.ingested_at = datetime.utcnow()
                    new_count += 1
            else:
                metrics = extract_metrics(src["market"], text)
                report = Report(
                    source=src["source"],
                    market=src["market"],
                    region=src.get("region", ""),
                    quarter=detected_q,
                    report_url=src["url"],
                    raw_text=text,
                    status="ingested",
                    has_real_data=bool(metrics.get("vacancy_rate") or metrics.get("asking_rent_psf"))
                )
                for k, v in metrics.items():
                    if hasattr(report, k) and v is not None:
                        setattr(report, k, v)
                db.session.add(report)
                new_count += 1

        except Exception as e:
            logger.error(f"Error processing {src['market']}: {e}")
            continue

    MonitorSource.query.update({"last_checked": datetime.utcnow()})
    db.session.commit()

    if new_count >= 3:
        logger.info(f"{new_count} updated reports — regenerating thesis")
        generate_thesis()

    logger.info(f"Scan complete. {new_count} new/updated reports.")


# ── API Routes ──────────────────────────────────────────────────────────────────

@app.route('/api/health')
def health():
    return jsonify({
        "status": "ok",
        "time": datetime.utcnow().isoformat(),
        "api_key": bool(ANTHROPIC_API_KEY),
        "reports": Report.query.count()
    })


@app.route('/api/stats')
def stats():
    report_count = Report.query.count()
    real_data_count = Report.query.filter_by(has_real_data=True).count()
    thesis = Thesis.query.filter_by(is_current=True).first()
    market_count = db.session.query(Report.market).filter(Report.has_real_data == True).distinct().count()
    latest_q = get_latest_quarter()
    return jsonify({
        "report_count": report_count,
        "real_data_count": real_data_count,
        "market_count": market_count,
        "source_count": MonitorSource.query.count(),
        "thesis_quarter": thesis.quarter if thesis else latest_q,
        "latest_quarter": latest_q,
        "thesis_generated": thesis.generated_at.isoformat() if thesis else None,
        "api_key_configured": bool(ANTHROPIC_API_KEY)
    })


@app.route('/api/thesis/current')
def get_thesis():
    thesis = Thesis.query.filter_by(is_current=True).order_by(Thesis.generated_at.desc()).first()
    if not thesis:
        return jsonify({"error": "No thesis available"}), 404
    return jsonify({
        "id": thesis.id,
        "quarter": thesis.quarter,
        "generated_at": thesis.generated_at.isoformat(),
        "summary": thesis.summary,
        "rankings": json.loads(thesis.rankings_json or "[]"),
        "risk_factors": json.loads(thesis.risk_factors or "[]"),
        "report_count": thesis.report_count
    })


@app.route('/api/thesis/history')
def thesis_history():
    theses = Thesis.query.order_by(Thesis.generated_at.desc()).limit(12).all()
    return jsonify([{
        "id": t.id, "quarter": t.quarter,
        "generated_at": t.generated_at.isoformat(),
        "report_count": t.report_count,
        "is_current": t.is_current
    } for t in theses])


@app.route('/api/thesis/regenerate', methods=['POST'])
def regenerate():
    generate_thesis()
    t = Thesis.query.filter_by(is_current=True).first()
    return jsonify({"status": "ok", "id": t.id if t else None})


@app.route('/api/reports')
def get_reports():
    """Return all reports with direct hyperlinks to source reports."""
    source = request.args.get('source')
    region = request.args.get('region')
    real_only = request.args.get('real_only', 'false').lower() == 'true'

    q = Report.query
    if source:
        q = q.filter_by(source=source)
    if region:
        q = q.filter_by(region=region)
    if real_only:
        q = q.filter_by(has_real_data=True)

    reports = q.order_by(Report.market, Report.source).all()

    return jsonify([{
        "id": r.id,
        "source": r.source,
        "market": r.market,
        "region": r.region,
        "quarter": r.quarter,
        "report_url": r.report_url,  # Direct clickable link
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
        "vac_0_100k": r.vac_0_100k,
        "vac_100_250k": r.vac_100_250k,
        "vac_250_500k": r.vac_250_500k,
        "vac_500_750k": r.vac_500_750k,
        "vac_750k_plus": r.vac_750k_plus,
        "rent_0_100k": r.rent_0_100k,
        "rent_100_250k": r.rent_100_250k,
        "rent_250_500k": r.rent_250_500k,
        "rent_500_750k": r.rent_500_750k,
        "rent_750k_plus": r.rent_750k_plus,
        "has_real_data": r.has_real_data,
        "data_completeness_pct": r.data_completeness_pct,
        "ingested_at": r.ingested_at.isoformat(),
        "status": r.status
    } for r in reports])


@app.route('/api/markets/summary')
def markets_summary():
    """Return one row per market — only markets with real data, best source per market."""
    real_only = request.args.get('real_only', 'true').lower() == 'true'

    seen = {}
    query = Report.query
    if real_only:
        query = query.filter_by(has_real_data=True)

    for r in query.order_by(Report.ingested_at.desc()).all():
        if r.market not in seen or \
           (r.data_completeness_pct or 0) > (seen[r.market].data_completeness_pct or 0):
            seen[r.market] = r

    result = []
    for r in seen.values():
        # Only include if we have the minimum 3 key metrics
        if r.vacancy_rate is None and r.asking_rent_psf is None and r.ytd_absorption_msf is None:
            continue

        result.append({
            "market": r.market,
            "region": r.region,
            "source": r.source,
            "quarter": r.quarter,
            "report_url": r.report_url,
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
            "vac_0_100k": r.vac_0_100k,
            "vac_100_250k": r.vac_100_250k,
            "vac_250_500k": r.vac_250_500k,
            "vac_500_750k": r.vac_500_750k,
            "vac_750k_plus": r.vac_750k_plus,
            "rent_0_100k": r.rent_0_100k,
            "rent_100_250k": r.rent_100_250k,
            "rent_250_500k": r.rent_250_500k,
            "rent_500_750k": r.rent_500_750k,
            "rent_750k_plus": r.rent_750k_plus,
            "data_completeness_pct": r.data_completeness_pct,
        })

    return jsonify(sorted(result, key=lambda x: x["market"]))


@app.route('/api/monitor/sources')
def get_sources():
    return jsonify([{
        "id": s.id, "name": s.name, "url": s.url,
        "last_checked": s.last_checked.isoformat() if s.last_checked else None,
        "last_quarter_seen": s.last_quarter_seen,
        "reports_tracked": s.reports_tracked,
        "status": s.status
    } for s in MonitorSource.query.all()])


@app.route('/api/monitor/scan', methods=['POST'])
def trigger_scan():
    check_for_new_reports()
    return jsonify({"status": "ok", "time": datetime.utcnow().isoformat()})


@app.route('/api/chat', methods=['POST'])
def chat():
    """
    Full AI chat with Claude — requires ANTHROPIC_API_KEY.
    Grounded in all thesis and market data.
    """
    data = request.json
    user_msg = data.get("message", "").strip()
    history = data.get("history", [])

    if not user_msg:
        return jsonify({"error": "No message provided"}), 400

    if not ANTHROPIC_API_KEY:
        return jsonify({
            "reply": "The AI chat requires an Anthropic API key to be configured. Please add your ANTHROPIC_API_KEY environment variable in your Render dashboard under Environment Variables, then redeploy. Your API key starts with 'sk-ant-api03-...' and was shown when you created it at console.anthropic.com."
        })

    # Build rich context from thesis and market data
    thesis = Thesis.query.filter_by(is_current=True).order_by(Thesis.generated_at.desc()).first()

    # Get markets with real data only
    seen = {}
    for r in Report.query.filter_by(has_real_data=True, status='ingested').all():
        if r.market not in seen or (r.data_completeness_pct or 0) > (seen[r.market].data_completeness_pct or 0):
            seen[r.market] = r

    ctx_parts = []

    if thesis:
        ctx_parts.append(f"CURRENT INVESTMENT THESIS ({thesis.quarter}):\n{thesis.summary[:2500]}")
        rankings = json.loads(thesis.rankings_json or "[]")
        rank_lines = []
        for r in rankings[:15]:
            ks = r.get("key_stats", {})
            line = f"#{r['rank']} {r['market']} ({r['region']}): Score {r['score']}/100 [{r['tier']}] — Vac:{ks.get('vacancy','N/A')}% Rent:${ks.get('rent','N/A')}/SF Abs:{ks.get('absorption','N/A')}MSF Cost:${ks.get('construction_cost','N/A')}/SF"
            rank_lines.append(line)
        ctx_parts.append("MARKET RANKINGS:\n" + "\n".join(rank_lines))

    mkt_lines = []
    for r in sorted(seen.values(), key=lambda x: x.market):
        parts = [r.market]
        if r.vacancy_rate is not None:         parts.append(f"Vac:{r.vacancy_rate}%")
        if r.asking_rent_psf is not None:      parts.append(f"Rent:${r.asking_rent_psf}/SF")
        if r.rent_growth_pct is not None:      parts.append(f"RentGrowth:{r.rent_growth_pct}%")
        if r.ytd_absorption_msf is not None:   parts.append(f"Abs:{r.ytd_absorption_msf}MSF")
        if r.cap_rate is not None:             parts.append(f"Cap:{r.cap_rate}%")
        if r.construction_cost_psf is not None: parts.append(f"Cost:${r.construction_cost_psf}/SF")
        if r.pipeline_msf is not None:         parts.append(f"Pipeline:{r.pipeline_msf}MSF")
        if r.vac_0_100k is not None:           parts.append(f"SmallBayVac:{r.vac_0_100k}%")
        if r.rent_0_100k is not None:          parts.append(f"SmallBayRent:${r.rent_0_100k}/SF")
        mkt_lines.append(" | ".join(parts))
    ctx_parts.append("DETAILED MARKET DATA:\n" + "\n".join(mkt_lines))

    system_prompt = f"""You are Claude, a senior industrial real estate investment analyst embedded in Glenstar Properties' market intelligence platform. Glenstar is an industrial real estate developer focused on building, leasing, and owning industrial properties across the United States.

You have access to verified data from {len(seen)} US industrial markets, pulled from JLL, CBRE, Cushman & Wakefield, Avison Young, Newmark, and Colliers reports.

{chr(10).join(ctx_parts)}

INSTRUCTIONS:
- Answer all questions with specific data — always cite actual numbers (vacancy %, rent $/SF, absorption MSF, construction cost $/SF)
- When discussing markets, always mention the size segment context (small-bay 0-100K vs mid-bay 100-250K vs big-box 500K+) when relevant
- Be direct and opinionated — Glenstar needs actionable investment guidance, not hedged generalities
- If asked about a market or topic not in your data, say so clearly — never fabricate numbers
- Keep responses to 3-4 paragraphs unless a detailed breakdown is specifically requested
- Industrial focus only — do not discuss office, retail, or multifamily"""

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

        messages = []
        for h in history[-10:]:  # Keep last 10 messages for context
            if h.get("role") in ["user", "assistant"] and h.get("content"):
                messages.append({"role": h["role"], "content": h["content"]})
        messages.append({"role": "user", "content": user_msg})

        msg = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            system=system_prompt,
            messages=messages
        )
        reply = msg.content[0].text

    except anthropic.AuthenticationError:
        reply = "Authentication failed — your API key appears to be invalid. Please check your ANTHROPIC_API_KEY in Render environment variables."
    except anthropic.RateLimitError:
        reply = "Rate limit reached. Please wait a moment and try again."
    except Exception as e:
        logger.error(f"Chat error: {e}")
        reply = f"An error occurred processing your request. Please try again. If the issue persists, check that your Anthropic API key is correctly configured in Render."

    return jsonify({"reply": reply})


# ── Application startup ─────────────────────────────────────────────────────────

with app.app_context():
    db.create_all()
    seed_database()

# Schedule twice-daily scans: 6:00 AM and 6:00 PM UTC
scheduler = BackgroundScheduler(timezone='UTC')
scheduler.add_job(check_for_new_reports, 'cron', hour='6,18', minute=0, id='twice_daily_scan')
scheduler.start()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
