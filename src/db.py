"""
Шар доступу до бази та завантаження довідників.
"""

from __future__ import annotations

import csv
import os
import sqlite3
import uuid
from datetime import datetime

BASE_DIR = os.path.join(os.path.dirname(__file__), '..')
DB_PATH = os.path.join(BASE_DIR, 'data', 'donors.db')
SCHEMA_PATH = os.path.join(BASE_DIR, 'sql', 'schema.sql')

BASE_CURRENCY = 'UAH'


def new_id(prefix: str) -> str:
    return f'{prefix}_{uuid.uuid4().hex[:12]}'


def now() -> str:
    return datetime.now().isoformat(timespec='seconds')


def connect(fresh: bool = False) -> sqlite3.Connection:
    if fresh and os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    if fresh:
        with open(SCHEMA_PATH, encoding='utf-8') as f:
            conn.executescript(f.read())
        _seed(conn)
        conn.commit()
    return conn


# --------------------------------------------------------------------------
# Довідники
# --------------------------------------------------------------------------

SOURCES = [
    # source_id, name, type, ingestion_mode, currency, fee%, fee_fixed
    ('src_website', 'Сайт (платіжний шлюз)', 'website', 'api_webhook', 'UAH', 2.5, 0),
    ('src_paypal', 'PayPal', 'paypal', 'file_import', 'USD', 3.4, 0.30),
    ('src_bank', 'Банківський переказ', 'bank_transfer', 'file_import', 'UAH', 0, 0),
    ('src_check', 'Чеки та готівка', 'check', 'manual_entry', 'UAH', 0, 0),
]

CAMPAIGNS = [
    ('winter_appeal_2024', 'Зимова кампанія 2024', 'annual', '2024-11-01', '2025-01-31', 500000),
    ('emergency_2025', 'Екстрений збір 2025', 'emergency', '2025-03-01', '2025-06-30', 2000000),
    ('scholarship_fund', 'Стипендіальний фонд', 'project', '2023-09-01', None, 1500000),
    ('annual_2025', 'Річна кампанія 2025', 'annual', '2025-01-01', '2025-12-31', 3000000),
    ('rebuild_2026', 'Відбудова 2026', 'project', '2026-01-01', None, 5000000),
]

FUNDS = [('general', 'Загальний фонд', 0),
         ('scholarship', 'Стипендії', 1),
         ('humanitarian', 'Гуманітарна допомога', 1)]


def _seed(conn: sqlite3.Connection):
    conn.executemany(
        'INSERT INTO source (source_id,name,source_type,ingestion_mode,'
        'default_currency,fee_percent,fee_fixed) VALUES (?,?,?,?,?,?,?)', SOURCES)
    conn.executemany(
        'INSERT INTO campaign (campaign_id,name,campaign_type,started_on,'
        'ended_on,goal_amount_base) VALUES (?,?,?,?,?,?)', CAMPAIGNS)
    conn.executemany(
        'INSERT INTO fund (fund_id,name,is_restricted) VALUES (?,?,?)', FUNDS)

    fx_path = os.path.join(BASE_DIR, 'data', 'fx_rates.csv')
    if os.path.exists(fx_path):
        with open(fx_path, encoding='utf-8') as f:
            rows = [(r['rate_date'], r['currency'], float(r['rate_to_base']), 'NBU')
                    for r in csv.DictReader(f)]
        conn.executemany(
            'INSERT INTO fx_rate (rate_date,currency,rate_to_base,source) '
            'VALUES (?,?,?,?)', rows)


def get_fx_rate(conn: sqlite3.Connection, currency: str, date_str: str) -> float:
    """
    Курс на дату транзакції, а не поточний.

    Якщо на точну дату курсу немає (вихідні, свята) — беремо останній
    доступний до цієї дати. Мовчазний фолбек на 1.0 неприпустимий:
    він перетворив би 100 USD на 100 UAH і зіпсував би всю аналітику.
    """
    if currency == BASE_CURRENCY:
        return 1.0
    row = conn.execute(
        'SELECT rate_to_base FROM fx_rate WHERE currency=? AND rate_date<=? '
        'ORDER BY rate_date DESC LIMIT 1', (currency, date_str)).fetchone()
    if row:
        return float(row['rate_to_base'])
    raise ValueError(f'Немає курсу {currency} на {date_str}')


def log_issue(conn, entity_type: str, entity_id: str, issue_type: str,
              severity: str, details: str = ''):
    conn.execute(
        'INSERT INTO data_quality_issue (issue_id,entity_type,entity_id,'
        'issue_type,severity,detected_at,details) VALUES (?,?,?,?,?,?,?)',
        (new_id('dq'), entity_type, entity_id, issue_type, severity, now(), details))
