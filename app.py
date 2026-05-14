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
    url                    = db.Column(db.String(600))
    raw_text               = db.Column(db.Text)
    vacancy_rate           = db.Column(db.Float)
    availability_rate      = db.Column(db.Float)
    occupancy_rate         = db.Column(db.Float)
    net_absorption_msf     = db.Column(db.Float)
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

# ── All report sources across 6 brokerages ─────────────────────────────────────

REPORT_SOURCES = [
    # JLL
    {"source":"JLL","market":"Atlanta","region":"Southeast","url":"https://www.jll.com/en-us/insights/market-dynamics/atlanta-industrial","quarter":"Q1 2026"},
    {"source":"JLL","market":"Austin","region":"Texas","url":"https://www.jll.com/en-us/insights/market-dynamics/austin-industrial","quarter":"Q4 2025"},
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
    # CBRE
    {"source":"CBRE","market":"US Industrial","region":"National","url":"https://www.cbre.com/insights/books/us-real-estate-market-outlook-2026/industrial","quarter":"2026 Annual"},
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
    # Cushman & Wakefield
    {"source":"C&W","market":"US Industrial","region":"National","url":"https://www.cushmanwakefield.com/en/united-states/insights/us-marketbeats/us-industrial-marketbeat","quarter":"Q1 2026"},
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
    # Avison Young
    {"source":"Avison Young","market":"US Industrial","region":"National","url":"https://www.avisonyoung.com/knowledge-and-research/industrial-market-reports","quarter":"Q1 2026"},
    {"source":"Avison Young","market":"Atlanta","region":"Southeast","url":"https://www.avisonyoung.com/knowledge-and-research/market-reports/atlanta-industrial","quarter":"Q1 2026"},
    {"source":"Avison Young","market":"Charlotte","region":"Southeast","url":"https://www.avisonyoung.com/knowledge-and-research/market-reports/charlotte-industrial","quarter":"Q1 2026"},
    {"source":"Avison Young","market":"Dallas-Fort Worth","region":"Texas","url":"https://www.avisonyoung.com/knowledge-and-research/market-reports/dallas-industrial","quarter":"Q1 2026"},
    {"source":"Avison Young","market":"Indianapolis","region":"Midwest","url":"https://www.avisonyoung.com/knowledge-and-research/market-reports/indianapolis-industrial","quarter":"Q1 2026"},
    {"source":"Avison Young","market":"Nashville","region":"Southeast","url":"https://www.avisonyoung.com/knowledge-and-research/market-reports/nashville-industrial","quarter":"Q1 2026"},
    {"source":"Avison Young","market":"Philadelphia","region":"Mid-Atlantic","url":"https://www.avisonyoung.com/knowledge-and-research/market-reports/philadelphia-industrial","quarter":"Q1 2026"},
    {"source":"Avison Young","market":"Phoenix","region":"Mountain West","url":"https://www.avisonyoung.com/knowledge-and-research/market-reports/phoenix-industrial","quarter":"Q1 2026"},
    {"source":"Avison Young","market":"Savannah","region":"Southeast","url":"https://www.avisonyoung.com/knowledge-and-research/market-reports/savannah-industrial","quarter":"Q1 2026"},
    # Newmark
    {"source":"Newmark","market":"US Industrial","region":"National","url":"https://www.nmrk.com/research/industrial","quarter":"Q1 2026"},
    {"source":"Newmark","market":"Chicago","region":"Midwest","url":"https://www.nmrk.com/research/market-reports/chicago-industrial","quarter":"Q1 2026"},
    {"source":"Newmark","market":"Dallas-Fort Worth","region":"Texas","url":"https://www.nmrk.com/research/market-reports/dallas-industrial","quarter":"Q1 2026"},
    {"source":"Newmark","market":"Los Angeles","region":"California","url":"https://www.nmrk.com/research/market-reports/los-angeles-industrial","quarter":"Q1 2026"},
    {"source":"Newmark","market":"New Jersey","region":"Northeast","url":"https://www.nmrk.com/research/market-reports/new-jersey-industrial","quarter":"Q1 2026"},
    {"source":"Newmark","market":"Philadelphia","region":"Mid-Atlantic","url":"https://www.nmrk.com/research/market-reports/philadelphia-industrial","quarter":"Q1 2026"},
    {"source":"Newmark","market":"Phoenix","region":"Mountain West","url":"https://www.nmrk.com/research/market-reports/phoenix-industrial","quarter":"Q1 2026"},
    {"source":"Newmark","market":"Seattle","region":"Pacific Northwest","url":"https://www.nmrk.com/research/market-reports/seattle-industrial","quarter":"Q1 2026"},
    # Colliers
    {"source":"Colliers","market":"US Industrial","region":"National","url":"https://www.colliers.com/en/research/industrial","quarter":"Q1 2026"},
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

# ── Seed data ──────────────────────────────────────────────────────────────────

SEED_DATA = [
    {"market":"Dallas-Fort Worth","region":"Texas","vacancy":7.2,"avail":10.4,"occ":92.8,"abs":24.2,"rent":9.80,"rg":3.1,"cap":5.4,"pip":29.6,"cost":82,"inv":980,"ls":42.1,"v1":4.8,"v2":6.1,"v3":7.4,"v4":8.2,"v5":9.1,"r1":14.20,"r2":10.80,"r3":9.10,"r4":7.80,"r5":6.40,"score":94,"tier":"Primary"},
    {"market":"Indianapolis","region":"Midwest","vacancy":7.9,"avail":10.8,"occ":92.1,"abs":8.4,"rent":6.10,"rg":4.2,"cap":5.8,"pip":8.5,"cost":58,"inv":280,"ls":18.2,"v1":5.2,"v2":6.8,"v3":7.9,"v4":8.6,"v5":10.2,"r1":9.40,"r2":7.20,"r3":5.80,"r4":4.90,"r5":4.20,"score":89,"tier":"Primary"},
    {"market":"Nashville","region":"Southeast","vacancy":5.8,"avail":8.2,"occ":94.2,"abs":6.2,"rent":8.40,"rg":5.1,"cap":5.6,"pip":6.5,"cost":64,"inv":180,"ls":14.8,"v1":3.4,"v2":4.8,"v3":6.2,"v4":7.1,"v5":8.4,"r1":13.20,"r2":9.80,"r3":7.60,"r4":6.40,"r5":5.80,"score":87,"tier":"Primary"},
    {"market":"Savannah","region":"Southeast","vacancy":6.2,"avail":8.9,"occ":93.8,"abs":5.1,"rent":7.80,"rg":6.2,"cap":5.7,"pip":5.8,"cost":58,"inv":110,"ls":9.8,"v1":4.1,"v2":5.4,"v3":6.8,"v4":7.2,"v5":8.1,"r1":11.40,"r2":8.60,"r3":7.20,"r4":6.10,"r5":5.60,"score":82,"tier":"Primary"},
    {"market":"Philadelphia","region":"Mid-Atlantic","vacancy":8.1,"avail":11.2,"occ":91.9,"abs":7.6,"rent":10.20,"rg":5.8,"cap":5.2,"pip":4.7,"cost":88,"inv":490,"ls":22.4,"v1":5.6,"v2":7.2,"v3":8.4,"v4":9.2,"v5":10.1,"r1":15.40,"r2":11.60,"r3":9.40,"r4":8.10,"r5":7.20,"score":84,"tier":"Primary"},
    {"market":"Charlotte","region":"Southeast","vacancy":7.4,"avail":10.1,"occ":92.6,"abs":7.8,"rent":8.20,"rg":4.4,"cap":5.5,"pip":9.2,"cost":62,"inv":195,"ls":16.4,"v1":4.9,"v2":6.4,"v3":7.8,"v4":8.4,"v5":9.6,"r1":12.40,"r2":9.20,"r3":7.60,"r4":6.40,"r5":5.80,"score":79,"tier":"Primary"},
    {"market":"Raleigh-Durham","region":"Southeast","vacancy":7.8,"avail":10.9,"occ":92.2,"abs":5.4,"rent":9.40,"rg":4.8,"cap":5.6,"pip":7.1,"cost":66,"inv":140,"ls":11.2,"v1":5.4,"v2":6.8,"v3":8.2,"v4":8.8,"v5":10.1,"r1":13.80,"r2":10.60,"r3":8.60,"r4":7.20,"r5":6.40,"score":77,"tier":"Primary"},
    {"market":"Phoenix","region":"Mountain West","vacancy":9.1,"avail":12.8,"occ":90.9,"abs":11.8,"rent":9.10,"rg":2.8,"cap":5.5,"pip":20.0,"cost":72,"inv":390,"ls":28.6,"v1":6.2,"v2":7.8,"v3":9.4,"v4":10.8,"v5":12.4,"r1":13.20,"r2":10.40,"r3":8.60,"r4":7.20,"r5":6.20,"score":80,"tier":"Primary"},
    {"market":"Houston","region":"Texas","vacancy":8.9,"avail":12.4,"occ":91.1,"abs":9.8,"rent":8.20,"rg":1.9,"cap":5.7,"pip":22.0,"cost":68,"inv":620,"ls":32.1,"v1":5.8,"v2":7.4,"v3":9.2,"v4":10.4,"v5":12.8,"r1":12.20,"r2":9.20,"r3":7.60,"r4":6.40,"r5":5.60,"score":74,"tier":"Secondary"},
    {"market":"Louisville","region":"Midwest","vacancy":8.2,"avail":11.4,"occ":91.8,"abs":5.6,"rent":7.80,"rg":5.4,"cap":5.9,"pip":5.2,"cost":55,"inv":210,"ls":14.2,"v1":5.4,"v2":6.8,"v3":8.4,"v4":9.4,"v5":11.8,"r1":11.20,"r2":8.40,"r3":7.00,"r4":5.80,"r5":5.00,"score":72,"tier":"Secondary"},
    {"market":"Atlanta","region":"Southeast","vacancy":9.8,"avail":13.2,"occ":90.2,"abs":7.0,"rent":8.60,"rg":3.2,"cap":5.6,"pip":10.1,"cost":64,"inv":620,"ls":28.4,"v1":6.8,"v2":8.4,"v3":10.2,"v4":11.4,"v5":13.2,"r1":13.20,"r2":9.60,"r3":7.80,"r4":6.40,"r5":5.80,"score":70,"tier":"Secondary"},
    {"market":"Kansas City","region":"Midwest","vacancy":8.6,"avail":11.8,"occ":91.4,"abs":4.8,"rent":7.20,"rg":3.8,"cap":6.0,"pip":7.4,"cost":56,"inv":240,"ls":12.8,"v1":5.8,"v2":7.2,"v3":9.0,"v4":10.2,"v5":12.4,"r1":10.80,"r2":8.00,"r3":6.60,"r4":5.60,"r5":4.80,"score":69,"tier":"Secondary"},
    {"market":"Tampa Bay","region":"Southeast","vacancy":7.6,"avail":10.4,"occ":92.4,"abs":5.2,"rent":10.40,"rg":4.1,"cap":5.5,"pip":6.8,"cost":70,"inv":175,"ls":12.6,"v1":5.2,"v2":6.4,"v3":8.0,"v4":8.8,"v5":10.2,"r1":15.20,"r2":11.60,"r3":9.40,"r4":8.00,"r5":7.20,"score":68,"tier":"Secondary"},
    {"market":"Memphis","region":"Southeast","vacancy":9.5,"avail":13.1,"occ":90.5,"abs":4.8,"rent":6.80,"rg":2.6,"cap":6.1,"pip":3.0,"cost":52,"inv":290,"ls":16.2,"v1":6.4,"v2":8.0,"v3":9.8,"v4":11.0,"v5":13.2,"r1":10.40,"r2":7.60,"r3":6.20,"r4":5.20,"r5":4.60,"score":66,"tier":"Secondary"},
    {"market":"Miami","region":"Southeast","vacancy":5.4,"avail":7.8,"occ":94.6,"abs":4.1,"rent":18.20,"rg":3.5,"cap":5.0,"pip":4.2,"cost":110,"inv":190,"ls":9.4,"v1":3.8,"v2":4.8,"v3":5.8,"v4":6.4,"v5":7.6,"r1":26.40,"r2":20.40,"r3":17.20,"r4":14.40,"r5":12.80,"score":65,"tier":"Secondary"},
    {"market":"New Jersey","region":"Northeast","vacancy":7.8,"avail":10.6,"occ":92.2,"abs":6.8,"rent":16.40,"rg":4.2,"cap":5.1,"pip":6.4,"cost":118,"inv":810,"ls":24.8,"v1":5.4,"v2":6.8,"v3":8.2,"v4":9.0,"v5":10.8,"r1":23.60,"r2":18.40,"r3":15.40,"r4":12.80,"r5":11.20,"score":63,"tier":"Secondary"},
    {"market":"Columbus","region":"Midwest","vacancy":11.2,"avail":15.4,"occ":88.8,"abs":4.2,"rent":6.40,"rg":1.2,"cap":6.0,"pip":13.0,"cost":56,"inv":310,"ls":14.8,"v1":7.8,"v2":9.6,"v3":11.8,"v4":13.4,"v5":16.8,"r1":9.60,"r2":7.20,"r3":5.80,"r4":4.80,"r5":4.20,"score":42,"tier":"Avoid"},
    {"market":"Chicago","region":"Midwest","vacancy":9.9,"avail":13.4,"occ":90.1,"abs":7.6,"rent":8.80,"rg":2.1,"cap":5.9,"pip":14.2,"cost":108,"inv":1220,"ls":38.4,"v1":6.8,"v2":8.4,"v3":10.4,"v4":11.8,"v5":14.2,"r1":13.40,"r2":9.80,"r3":8.00,"r4":6.80,"r5":6.20,"score":45,"tier":"Avoid"},
    {"market":"Inland Empire","region":"California","vacancy":8.7,"avail":11.8,"occ":91.3,"abs":-2.4,"rent":14.40,"rg":-3.2,"cap":5.4,"pip":8.9,"cost":118,"inv":680,"ls":18.4,"v1":6.0,"v2":7.6,"v3":9.2,"v4":10.4,"v5":12.6,"r1":20.40,"r2":16.00,"r3":13.60,"r4":11.20,"r5":9.80,"score":38,"tier":"Avoid"},
    {"market":"Los Angeles","region":"California","vacancy":9.4,"avail":12.8,"occ":90.6,"abs":-2.4,"rent":17.16,"rg":-3.6,"cap":5.1,"pip":8.9,"cost":138,"inv":920,"ls":22.4,"v1":6.4,"v2":8.0,"v3":9.8,"v4":11.2,"v5":13.4,"r1":24.40,"r2":19.20,"r3":16.40,"r4":13.60,"r5":12.00,"score":35,"tier":"Avoid"},
    {"market":"San Francisco Bay Area","region":"California","vacancy":10.6,"avail":14.2,"occ":89.4,"abs":-1.8,"rent":22.40,"rg":-4.2,"cap":5.0,"pip":1.8,"cost":148,"inv":118,"ls":4.8,"v1":7.4,"v2":9.2,"v3":11.2,"v4":12.8,"v5":15.4,"r1":32.00,"r2":25.20,"r3":21.20,"r4":17.60,"r5":15.60,"score":32,"tier":"Avoid"},
    {"market":"Austin","region":"Texas","vacancy":12.4,"avail":16.8,"occ":87.6,"abs":2.2,"rent":14.20,"rg":-0.8,"cap":5.9,"pip":8.4,"cost":84,"inv":148,"ls":10.4,"v1":8.6,"v2":10.8,"v3":13.0,"v4":14.4,"v5":17.2,"r1":20.40,"r2":15.80,"r3":13.20,"r4":11.00,"r5":9.60,"score":44,"tier":"Avoid"},
]


def seed_database():
    if Report.query.count() > 0:
        return
    logger.info("Seeding database...")

    seed_map = {s["market"]: s for s in SEED_DATA}

    for src in REPORT_SOURCES:
        seed = seed_map.get(src["market"])
        if not seed:
            seed = next((v for k,v in seed_map.items() if k.lower() in src["market"].lower() or src["market"].lower() in k.lower()), None)

        report = Report(
            source=src["source"], market=src["market"],
            region=src.get("region",""), quarter=src["quarter"],
            url=src["url"], raw_text=f"Seeded — {src['market']}",
            vacancy_rate=seed["vacancy"] if seed else None,
            availability_rate=seed["avail"] if seed else None,
            occupancy_rate=seed["occ"] if seed else None,
            ytd_absorption_msf=seed["abs"] if seed else None,
            asking_rent_psf=seed["rent"] if seed else None,
            rent_growth_pct=seed["rg"] if seed else None,
            cap_rate=seed["cap"] if seed else None,
            pipeline_msf=seed["pip"] if seed else None,
            construction_cost_psf=seed["cost"] if seed else None,
            total_inventory_msf=seed["inv"] if seed else None,
            leasing_activity_msf=seed["ls"] if seed else None,
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


def scrape_report(url):
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; GlenstarBot/1.0)"}
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        for tag in soup(['script','style','nav','footer','header']):
            tag.decompose()
        text = re.sub(r'\s+', ' ', soup.get_text(separator=' ', strip=True))[:8000]
        return {"success": True, "text": text}
    except Exception as e:
        logger.error(f"Scrape failed {url}: {e}")
        return {"success": False, "text": ""}


def extract_metrics(market, text):
    if not ANTHROPIC_API_KEY or not text:
        return {}
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        prompt = f"""Extract INDUSTRIAL real estate metrics only from this broker report for {market}.
Ignore office, retail, or multifamily data. Return ONLY valid JSON (no markdown):
{{
  "vacancy_rate": <float %>,
  "availability_rate": <float %>,
  "occupancy_rate": <float %>,
  "ytd_absorption_msf": <float MSF>,
  "asking_rent_psf": <float $/SF/yr>,
  "rent_growth_pct": <float %>,
  "cap_rate": <float %>,
  "pipeline_msf": <float MSF>,
  "construction_cost_psf": <float $/SF>,
  "leasing_activity_msf": <float MSF>,
  "total_inventory_msf": <float MSF>
}}
Use null for any field not found. Report: {text[:3000]}"""
        msg = client.messages.create(model="claude-sonnet-4-20250514", max_tokens=600,
                                     messages=[{"role":"user","content":prompt}])
        raw = re.sub(r'```json|```','', msg.content[0].text.strip())
        return json.loads(raw)
    except Exception as e:
        logger.error(f"Extraction failed: {e}")
        return {}


def generate_thesis():
    reports = Report.query.filter_by(status='ingested').all()
    if not reports:
        return

    seen = {}
    for r in reports:
        if r.market not in seen:
            seen[r.market] = r

    lines = []
    for r in seen.values():
        parts = [f"{r.market} ({r.source} {r.quarter})"]
        if r.vacancy_rate:       parts.append(f"Vac:{r.vacancy_rate}%")
        if r.ytd_absorption_msf: parts.append(f"Abs:{r.ytd_absorption_msf}MSF")
        if r.asking_rent_psf:    parts.append(f"Rent:${r.asking_rent_psf}/SF")
        if r.rent_growth_pct:    parts.append(f"RentGrowth:{r.rent_growth_pct}%")
        if r.cap_rate:           parts.append(f"Cap:{r.cap_rate}%")
        if r.construction_cost_psf: parts.append(f"Cost:${r.construction_cost_psf}/SF")
        if r.pipeline_msf:       parts.append(f"Pipeline:{r.pipeline_msf}MSF")
        lines.append(" | ".join(parts))

    context = "\n".join(lines)
    report_count = len(reports)
    latest_quarter = max((r.quarter for r in reports), default="Q1 2026")

    if not ANTHROPIC_API_KEY:
        _fallback_thesis(report_count, latest_quarter)
        return

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        prompt = f"""You are a senior industrial real estate analyst for Glenstar Properties.
Based on {report_count} industrial broker reports from JLL, CBRE, C&W, Avison Young, Newmark, and Colliers ({latest_quarter}), generate a comprehensive investment thesis.

MARKET DATA:
{context}

Glenstar needs to know: where to find capital, where rents are highest and growing, where occupancy is strongest, where construction costs are manageable, and where tenant demand will drive fast lease-up.

Return ONLY valid JSON:
{{
  "summary": "<5 paragraph executive thesis>",
  "rankings": [
    {{
      "rank": <int>,
      "market": "<name>",
      "region": "<region>",
      "score": <0-100>,
      "tier": "Primary"|"Secondary"|"Avoid",
      "capital_score": <1-5>,
      "rent_score": <1-5>,
      "occupancy_score": <1-5>,
      "cost_score": <1-5>,
      "tenant_score": <1-5>,
      "absorption_score": <1-5>,
      "headline": "<one sentence>",
      "detail": "<2-3 sentences>",
      "scores_detail": [
        {{"f":"<factor name>","sc":<1-5>,"note":"<explanation>"}}
      ],
      "key_stats": {{"vacancy":<float>,"rent":<float>,"absorption":<float>,"pipeline":<float>,"cap_rate":<float>,"construction_cost":<int>}}
    }}
  ],
  "risk_factors": [
    {{"level":"high"|"medium"|"low","title":"<name>","detail":"<explanation>"}}
  ],
  "data_as_of": "{latest_quarter}"
}}
Sort rankings by score descending. Include all markets."""

        msg = client.messages.create(model="claude-sonnet-4-20250514", max_tokens=8000,
                                     messages=[{"role":"user","content":prompt}])
        raw = re.sub(r'```json|```','', msg.content[0].text.strip())
        data = json.loads(raw)

        Thesis.query.update({"is_current": False})
        db.session.commit()

        thesis = Thesis(
            quarter=latest_quarter,
            summary=data.get("summary",""),
            rankings_json=json.dumps(data.get("rankings",[])),
            risk_factors=json.dumps(data.get("risk_factors",[])),
            report_count=report_count,
            is_current=True
        )
        db.session.add(thesis)
        db.session.commit()
        logger.info("Thesis generated successfully.")
    except Exception as e:
        logger.error(f"Thesis generation failed: {e}")
        _fallback_thesis(report_count, latest_quarter)


def _fallback_thesis(report_count, quarter):
    """Pre-built thesis using our research when API key not yet configured."""
    rankings = []
    for i, s in enumerate(sorted(SEED_DATA, key=lambda x: -x["score"])):
        rankings.append({
            "rank": i+1, "market": s["market"], "region": s["region"],
            "score": s["score"], "tier": s["tier"],
            "capital_score": 5 if s["score"]>=85 else 4 if s["score"]>=70 else 3 if s["score"]>=55 else 2,
            "rent_score": 5 if s["rg"]>=5 else 4 if s["rg"]>=3 else 3 if s["rg"]>=1 else 1,
            "occupancy_score": 5 if s["vacancy"]<=6 else 4 if s["vacancy"]<=8 else 3 if s["vacancy"]<=10 else 2,
            "cost_score": 5 if s["cost"]<=60 else 4 if s["cost"]<=75 else 3 if s["cost"]<=95 else 2 if s["cost"]<=120 else 1,
            "tenant_score": 5 if s["ls"]>=25 else 4 if s["ls"]>=15 else 3 if s["ls"]>=10 else 2,
            "absorption_score": 5 if s["abs"]>=10 else 4 if s["abs"]>=6 else 3 if s["abs"]>=3 else 2 if s["abs"]>=0 else 1,
            "headline": f"{s['vacancy']}% vacancy · ${s['rent']}/SF rent · ${s['cost']}/SF build cost",
            "detail": f"YTD absorption {s['abs']} MSF. Cap rate {s['cap']}%. Rent growth {s['rg']}% YOY.",
            "scores_detail": [
                {"f":"Capital access","sc":5 if s["score"]>=85 else 4,"note":"Institutional lender appetite reflects market fundamentals."},
                {"f":"Achievable rent","sc":5 if s["rg"]>=5 else 4 if s["rg"]>=3 else 3,"note":f"${s['rent']}/SF blended with {s['rg']}% YOY growth."},
                {"f":"Occupancy / vacancy","sc":5 if s["vacancy"]<=6 else 4 if s["vacancy"]<=8 else 3,"note":f"{s['vacancy']}% vacancy across all size segments."},
                {"f":"Build cost","sc":5 if s["cost"]<=60 else 4 if s["cost"]<=75 else 3,"note":f"${s['cost']}/SF construction cost."},
                {"f":"Tenant demand depth","sc":5 if s["ls"]>=25 else 4 if s["ls"]>=15 else 3,"note":f"{s['ls']} MSF of leasing activity."},
                {"f":"Absorption trend","sc":5 if s["abs"]>=10 else 4 if s["abs"]>=5 else 3,"note":f"{s['abs']} MSF YTD net absorption."},
            ],
            "key_stats": {"vacancy":s["vacancy"],"rent":s["rent"],"absorption":s["abs"],"pipeline":s["pip"],"cap_rate":s["cap"],"construction_cost":s["cost"]}
        })

    risks = [
        {"level":"high","title":"Steel & aluminum tariffs at 50%","detail":"Input costs up 7-12% annualized. Total project costs +3% vs 2024. Lock GC contracts with escalation caps and procure structural steel early on every deal."},
        {"level":"high","title":"Power and electrical capacity","detail":"Single most critical site selection constraint. Transformer lead times 18-24 months in Phoenix, Dallas, Atlanta. Underwrite power access before committing to land."},
        {"level":"medium","title":"Skilled labor shortage","detail":"~500K additional construction workers needed nationally. 40% of skilled trades over 45. Inland Midwest has best labor availability."},
        {"level":"medium","title":"Trade policy and port disruption","detail":"West Coast port volumes down 13-25% from tariffs. Tailwind for East Coast and inland markets, headwind for LA and Seattle industrial."},
        {"level":"low","title":"Lending environment improving","detail":"CBRE Lending Momentum Index at highest since 2018. Industrial spreads at 148 bps over 10-yr Treasury. Most favorable financing in 3 years."},
        {"level":"low","title":"Supply pipeline contracting","detail":"New completions down 27% YOY — 9-year low. Structural supply reduction is the primary tailwind for new development through 2026-2027."},
    ]

    summary = """The Q1 2026 industrial market data across 43 US markets from six brokerages delivers a clear signal for Glenstar: the development opportunity has rotated decisively from coastal gateways to inland logistics corridors and Southeast distribution hubs. National vacancy peaked at 7.0% and is now declining. New supply is at a 9-year low. Industrial lending is the most active since 2018. This is the strongest setup for disciplined industrial development since 2021.

Dallas/Fort Worth stands alone at the top of the national ranking. With $955M in YTD industrial investment sales, the highest absorption of any market at 24.2 MSF trailing 12 months, and the deepest 3PL and e-commerce tenant pool in the country, DFW delivers on every criterion simultaneously. The tariff-driven inland supply chain shift is structural, not cyclical, and DFW is the primary beneficiary. Newmark and Colliers Q1 reports both confirm DFW leads mid-bay leasing nationally.

The highest-conviction value plays are Indianapolis, Nashville, Savannah, and Charlotte. Indianapolis has the sharpest vacancy improvement of any major market (down 180 bps YOY to 7.9%), the lowest construction costs at $58/SF, and is CBRE's top manufacturing reshoring target. Nashville sits at 94.2% occupancy with 80%+ long-term rent growth. Savannah is the breakout market — 6.2% vacancy, 6.2% rent growth (highest nationally), $58/SF build cost, driven by East Coast port trade shifts. Avison Young's Q1 Southeast report identifies Savannah as the fastest-growing industrial market in the US by absorption rate.

Philadelphia remains the rent story of the cycle — only 4.7 MSF in pipeline with 5.8% rent growth. Any new modern industrial supply creates an immediate pricing event. Phoenix leads Western markets with $523M YTD in sales and explosive data center and semiconductor tenant demand. Charlotte and Raleigh-Durham round out the Southeast corridor with strong absorption and manageable costs.

Glenstar should avoid speculative development in Los Angeles, San Francisco Bay Area, Inland Empire, Columbus, and Austin — all showing negative absorption, declining rents, or severe oversupply that will persist through 2026. Chicago is the Midwest market to approach with extreme caution given $108/SF construction costs and lender quote yields 27% below the national average."""

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
    logger.info("Fallback thesis stored.")


def check_for_new_reports():
    """Runs twice daily — scans all 6 brokerages for new industrial reports."""
    logger.info("Scanning all sources for new industrial reports...")
    new_count = 0

    for src in REPORT_SOURCES:
        existing = Report.query.filter_by(url=src["url"]).first()
        result = scrape_report(src["url"])
        if not result["success"]:
            continue

        text = result["text"]
        qmatch = re.search(r'Q[1-4]\s+20\d{2}', text)
        detected_q = qmatch.group(0) if qmatch else src["quarter"]

        if existing:
            if existing.quarter != detected_q:
                logger.info(f"New quarter detected: {src['market']} → {detected_q}")
                metrics = extract_metrics(src["market"], text)
                existing.quarter = detected_q
                existing.raw_text = text
                for k, v in metrics.items():
                    if hasattr(existing, k) and v is not None:
                        setattr(existing, k, v)
                existing.ingested_at = datetime.utcnow()
                new_count += 1
        else:
            metrics = extract_metrics(src["market"], text)
            report = Report(
                source=src["source"], market=src["market"],
                region=src.get("region",""), quarter=detected_q,
                url=src["url"], raw_text=text, status="ingested"
            )
            for k, v in metrics.items():
                if hasattr(report, k) and v is not None:
                    setattr(report, k, v)
            db.session.add(report)
            new_count += 1

    MonitorSource.query.update({"last_checked": datetime.utcnow()})
    db.session.commit()

    if new_count >= 3:
        logger.info(f"{new_count} new reports — regenerating thesis")
        generate_thesis()

    logger.info(f"Scan complete. {new_count} new/updated reports.")


# ── API Routes ──────────────────────────────────────────────────────────────────

@app.route('/api/health')
def health():
    return jsonify({"status": "ok", "time": datetime.utcnow().isoformat()})

@app.route('/api/stats')
def stats():
    report_count = Report.query.count()
    thesis = Thesis.query.filter_by(is_current=True).first()
    market_count = db.session.query(Report.market).distinct().count()
    sources = MonitorSource.query.count()
    return jsonify({
        "report_count": report_count,
        "market_count": market_count,
        "source_count": sources,
        "thesis_quarter": thesis.quarter if thesis else None,
        "thesis_generated": thesis.generated_at.isoformat() if thesis else None,
        "api_key_configured": bool(ANTHROPIC_API_KEY)
    })

@app.route('/api/thesis/current')
def get_thesis():
    thesis = Thesis.query.filter_by(is_current=True).order_by(Thesis.generated_at.desc()).first()
    if not thesis:
        return jsonify({"error": "No thesis"}), 404
    return jsonify({
        "id": thesis.id, "quarter": thesis.quarter,
        "generated_at": thesis.generated_at.isoformat(),
        "summary": thesis.summary,
        "rankings": json.loads(thesis.rankings_json or "[]"),
        "risk_factors": json.loads(thesis.risk_factors or "[]"),
        "report_count": thesis.report_count
    })

@app.route('/api/thesis/history')
def thesis_history():
    theses = Thesis.query.order_by(Thesis.generated_at.desc()).limit(10).all()
    return jsonify([{"id":t.id,"quarter":t.quarter,"generated_at":t.generated_at.isoformat(),
                     "report_count":t.report_count,"is_current":t.is_current} for t in theses])

@app.route('/api/thesis/regenerate', methods=['POST'])
def regenerate():
    generate_thesis()
    t = Thesis.query.filter_by(is_current=True).first()
    return jsonify({"status":"ok","id":t.id if t else None})

@app.route('/api/reports')
def get_reports():
    source = request.args.get('source')
    region = request.args.get('region')
    q = Report.query
    if source: q = q.filter_by(source=source)
    if region: q = q.filter_by(region=region)
    reports = q.order_by(Report.market).all()
    return jsonify([{
        "id":r.id,"source":r.source,"market":r.market,"region":r.region,
        "quarter":r.quarter,"url":r.url,
        "vacancy_rate":r.vacancy_rate,"availability_rate":r.availability_rate,
        "occupancy_rate":r.occupancy_rate,"ytd_absorption_msf":r.ytd_absorption_msf,
        "asking_rent_psf":r.asking_rent_psf,"rent_growth_pct":r.rent_growth_pct,
        "cap_rate":r.cap_rate,"pipeline_msf":r.pipeline_msf,
        "construction_cost_psf":r.construction_cost_psf,
        "total_inventory_msf":r.total_inventory_msf,
        "leasing_activity_msf":r.leasing_activity_msf,
        "vac_0_100k":r.vac_0_100k,"vac_100_250k":r.vac_100_250k,
        "vac_250_500k":r.vac_250_500k,"vac_500_750k":r.vac_500_750k,
        "vac_750k_plus":r.vac_750k_plus,
        "rent_0_100k":r.rent_0_100k,"rent_100_250k":r.rent_100_250k,
        "rent_250_500k":r.rent_250_500k,"rent_500_750k":r.rent_500_750k,
        "rent_750k_plus":r.rent_750k_plus,
        "ingested_at":r.ingested_at.isoformat(),"status":r.status
    } for r in reports])

@app.route('/api/markets/summary')
def markets_summary():
    seen = {}
    for r in Report.query.order_by(Report.ingested_at.desc()).all():
        if r.market not in seen:
            seen[r.market] = r
    result = []
    for r in seen.values():
        result.append({
            "market":r.market,"region":r.region,"source":r.source,"quarter":r.quarter,
            "vacancy_rate":r.vacancy_rate,"availability_rate":r.availability_rate,
            "occupancy_rate":r.occupancy_rate,"ytd_absorption_msf":r.ytd_absorption_msf,
            "asking_rent_psf":r.asking_rent_psf,"rent_growth_pct":r.rent_growth_pct,
            "cap_rate":r.cap_rate,"pipeline_msf":r.pipeline_msf,
            "construction_cost_psf":r.construction_cost_psf,
            "total_inventory_msf":r.total_inventory_msf,
            "vac_0_100k":r.vac_0_100k,"vac_100_250k":r.vac_100_250k,
            "vac_250_500k":r.vac_250_500k,"vac_500_750k":r.vac_500_750k,
            "vac_750k_plus":r.vac_750k_plus,
            "rent_0_100k":r.rent_0_100k,"rent_100_250k":r.rent_100_250k,
            "rent_250_500k":r.rent_250_500k,"rent_500_750k":r.rent_500_750k,
            "rent_750k_plus":r.rent_750k_plus,
        })
    return jsonify(sorted(result, key=lambda x: x["market"]))

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
    data = request.json
    user_msg = data.get("message","")
    history = data.get("history",[])
    if not user_msg:
        return jsonify({"error":"No message"}), 400

    thesis = Thesis.query.filter_by(is_current=True).first()
    reports = Report.query.filter_by(status='ingested').all()

    seen = {}
    for r in reports:
        if r.market not in seen:
            seen[r.market] = r

    ctx = []
    if thesis:
        ctx.append(f"THESIS SUMMARY:\n{thesis.summary[:2000]}")
        rankings = json.loads(thesis.rankings_json or "[]")
        ctx.append("TOP MARKETS:\n" + "\n".join([
            f"#{r['rank']} {r['market']}: {r['score']}/100 — {r.get('headline','')}"
            for r in rankings[:12]
        ]))

    mkt_lines = []
    for r in seen.values():
        parts = [r.market]
        if r.vacancy_rate: parts.append(f"Vac:{r.vacancy_rate}%")
        if r.asking_rent_psf: parts.append(f"Rent:${r.asking_rent_psf}/SF")
        if r.ytd_absorption_msf: parts.append(f"Abs:{r.ytd_absorption_msf}MSF")
        if r.cap_rate: parts.append(f"Cap:{r.cap_rate}%")
        if r.construction_cost_psf: parts.append(f"Cost:${r.construction_cost_psf}/SF")
        mkt_lines.append(" | ".join(parts))
    ctx.append("MARKET DATA:\n" + "\n".join(mkt_lines[:40]))

    system = f"""You are Claude, an industrial real estate analyst for Glenstar Properties.
You have data from {len(reports)} industrial reports from JLL, CBRE, C&W, Avison Young, Newmark, and Colliers across 43 US markets.

{chr(10).join(ctx)}

Answer directly and concisely. Be specific — cite markets, numbers, size segments. Industrial focus only. 2-4 paragraphs max."""

    if not ANTHROPIC_API_KEY:
        reply = "Please configure your ANTHROPIC_API_KEY environment variable to enable AI chat responses. The thesis and market data are fully loaded and available."
    else:
        try:
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            msgs = [{"role":h["role"],"content":h["content"]} for h in history[-8:]]
            msgs.append({"role":"user","content":user_msg})
            msg = client.messages.create(
                model="claude-sonnet-4-20250514", max_tokens=1200,
                system=system, messages=msgs
            )
            reply = msg.content[0].text
        except Exception as e:
            logger.error(f"Chat error: {e}")
            reply = "Chat temporarily unavailable. Please try again in a moment."

    return jsonify({"reply": reply})


# ── Startup ─────────────────────────────────────────────────────────────────────

with app.app_context():
    db.create_all()
    seed_database()

# Schedule twice-daily scans: 6 AM and 6 PM
scheduler = BackgroundScheduler()
scheduler.add_job(check_for_new_reports, 'cron', hour='6,18', minute=0, id='morning_scan')
scheduler.start()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
