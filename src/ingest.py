"""
Пайплайн обробки пожертв.

    файл каналу
        -> перевірка на повторне завантаження (хеш файлу)
        -> raw_donation (незмінний журнал)
        -> парсер каналу
        -> валідація
        -> перерахунок валюти на дату
        -> матчинг донора
        -> створення / оновлення профілю
        -> запис пожертви

Кожен крок логується в automation_run, кожна проблема — в
data_quality_issue або match_review_queue. Тихих збоїв бути не має.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))

import db
from db import BASE_CURRENCY, connect, get_fx_rate, log_issue, new_id, now
from matching import DonorMatcher, candidates_json
from normalize import (normalize_email, normalize_name, normalize_phone,
                       parse_amount, parse_date, transliterate)

INCOMING = os.path.join(os.path.dirname(__file__), '..', 'data', 'incoming')

# Маркери, за якими донор розпізнається як організація.
# Для них інша логіка звернення в комунікаціях: не "Дорога Олено",
# а "Шановні партнери", і подяка йде контактній особі, а не "компанії".
ORG_MARKERS = ('тов ', 'пп ', 'фоп ', 'llc', 'ltd', 'inc', 'foundation',
               'gmbh', 'corp', 'company', 'фонд')

ANONYMOUS_DONOR_ID = 'donor_anonymous'


# ==========================================================================
# ПАРСЕРИ КАНАЛІВ
# ==========================================================================
# Кожен парсер приводить свій формат до єдиної структури.
# Уся канал-специфічна логіка живе тільки тут: додати п'ятий канал
# означає написати один парсер, не чіпаючи решту пайплайна.

def parse_website(row: dict) -> dict:
    amount, _ = parse_amount(row.get('amount'))
    return dict(
        external_id=row.get('transaction_id', '').strip(),
        occurred_at=parse_date(row.get('created_at')),
        name=row.get('donor_name', '').strip(),
        email=row.get('donor_email', '').strip(),
        phone=row.get('donor_phone', '').strip(),
        processor_id=None, bank_account_hash=None,
        amount=amount, currency=(row.get('currency') or 'UAH').strip().upper(),
        fee=None,
        payment_method=row.get('payment_method'),
        campaign=row.get('campaign') or None,
        fund=row.get('fund') or None,
        is_recurring=row.get('is_recurring') == 'true',
        subscription_id=None,
        status='completed',
        refund_of=None,
        consent=row.get('marketing_consent') == 'true',
        consent_known=True,          # сайт — єдиний канал, де згода питається явно
        country=row.get('country'), city=row.get('city'),
    )


def parse_paypal(row: dict) -> dict:
    gross, cur_from_amt = parse_amount(row.get('Gross'))
    fee, _ = parse_amount(row.get('Fee'))
    status_map = {'Completed': 'completed', 'Refunded': 'refunded',
                  'Failed': 'failed', 'Pending': 'pending'}
    return dict(
        external_id=row.get('Transaction ID', '').strip(),
        occurred_at=parse_date(row.get('Date')),
        name=row.get('Name', '').strip(),
        email=row.get('From Email Address', '').strip(),
        phone=None,
        processor_id=(row.get('Payer ID') or '').strip().lower() or None,
        bank_account_hash=None,
        amount=gross,
        currency=(row.get('Currency') or cur_from_amt or 'USD').strip().upper(),
        fee=abs(fee) if fee else 0.0,
        payment_method='paypal',
        campaign=None, fund=None,
        is_recurring=bool((row.get('Subscription ID') or '').strip()),
        subscription_id=(row.get('Subscription ID') or '').strip() or None,
        status=status_map.get(row.get('Status'), 'completed'),
        refund_of=(row.get('Reference Txn ID') or '').strip() or None,
        consent=False,
        consent_known=False,         # PayPal не питає згоду на розсилку фонду
        country=row.get('Country'), city=None,
    )


def parse_bank(row: dict) -> dict:
    amount, _ = parse_amount(row.get('Сума'))
    iban = (row.get('IBAN платника') or '').strip().replace(' ', '').upper()
    return dict(
        external_id=row.get('Референс', '').strip(),
        occurred_at=parse_date(row.get('Дата операції')),
        name=row.get('Платник', '').strip(),
        email=None, phone=None, processor_id=None,
        # IBAN не зберігаємо у відкритому вигляді: це платіжний реквізит.
        # Для матчингу достатньо стабільного хешу.
        bank_account_hash=hashlib.sha256(iban.encode()).hexdigest()[:32] if iban else None,
        amount=amount, currency=(row.get('Валюта') or 'UAH').strip().upper(),
        fee=0.0, payment_method='bank_transfer',
        campaign=None, fund=None, is_recurring=False, subscription_id=None,
        status='completed', refund_of=None,
        consent=False, consent_known=False,
        country='UA', city=None,
    )


def parse_check(row: dict) -> dict:
    amount, _ = parse_amount(row.get('amount'))
    donor = (row.get('donor') or '').strip()
    city = None
    addr = row.get('address') or ''
    for marker in ('м. ', 'м.'):
        if marker in addr:
            city = addr.split(marker, 1)[1].split(',')[0].strip()
            break
    return dict(
        external_id=row.get('check_no', '').strip(),
        occurred_at=parse_date(row.get('date_received')),
        name=donor, email=None, phone=None,
        processor_id=None, bank_account_hash=None,
        amount=amount, currency=(row.get('currency') or 'UAH').strip().upper(),
        fee=0.0, payment_method='check',
        campaign=None, fund=None, is_recurring=False, subscription_id=None,
        status='completed', refund_of=None,
        consent=False, consent_known=False,
        country='UA' if city else None, city=city,
        is_anonymous=not donor,
    )


CHANNELS = {
    'website_donations.csv': ('src_website', parse_website),
    'paypal_transactions.csv': ('src_paypal', parse_paypal),
    'paypal_transactions_REIMPORT.csv': ('src_paypal', parse_paypal),
    'bank_statement.csv': ('src_bank', parse_bank),
    'checks_manual.csv': ('src_check', parse_check),
}


# ==========================================================================
# ВАЛІДАЦІЯ
# ==========================================================================

def validate(rec: dict) -> list[str]:
    """
    Повертає список фатальних помилок. Порожній список — запис придатний.

    Свідомо розрізняємо фатальне і неповне. Відсутній email — не привід
    відкидати пожертву: гроші прийшли, і вони мають бути у звітності.
    А от сума, яку не вдалося розібрати, — фатально: записати нуль
    означало б тихо втратити гроші зі звітів.
    """
    errors = []
    if rec.get('amount') is None:
        errors.append('unparseable_amount')
    elif rec['amount'] == 0:
        errors.append('zero_amount')
    if rec.get('occurred_at') is None:
        errors.append('unparseable_date')
    if not rec.get('external_id'):
        errors.append('missing_external_id')
    if rec.get('currency') not in ('UAH', 'USD', 'EUR', 'GBP', 'PLN'):
        errors.append('unknown_currency')
    return errors


def soft_warnings(rec: dict) -> list[str]:
    """Неповнота, яку варто рахувати, але яка не блокує обробку."""
    w = []
    if not rec.get('email'):
        w.append('missing_email')
    if not rec.get('phone'):
        w.append('missing_phone')
    if not rec.get('name') and not rec.get('is_anonymous'):
        w.append('missing_name')
    return w


# ==========================================================================
# ЗАПИС ДОНОРА
# ==========================================================================

def is_organization(name: str) -> bool:
    low = (name or '').lower()
    return any(m in low for m in ORG_MARKERS)


def ensure_anonymous_donor(conn):
    """
    Один системний профіль для анонімних пожертв.

    Анонімні гроші мають потрапити у фінансову звітність, але не
    створювати фантомних донорів у CRM. Без цього запису або
    втрачаються гроші, або база засмічується профілями-привидами.
    """
    if conn.execute('SELECT 1 FROM donor WHERE donor_id=?',
                    (ANONYMOUS_DONOR_ID,)).fetchone():
        return
    conn.execute(
        'INSERT INTO donor (donor_id,donor_type,display_name,name_normalized,'
        'donor_status,created_at,updated_at,created_by) VALUES (?,?,?,?,?,?,?,?)',
        (ANONYMOUS_DONOR_ID, 'anonymous', 'Анонімні пожертви', None,
         'do_not_contact', now(), now(), 'system'))


def create_donor(conn, matcher: DonorMatcher, rec: dict, flags: list[str]) -> str:
    donor_id = new_id('donor')
    name = rec.get('name') or ''
    org = is_organization(name)
    name_norm = normalize_name(name)

    first = last = None
    if not org and name:
        parts = [p for p in name.replace(',', ' ').split() if len(p) > 1]
        if len(parts) >= 2:
            # У кирилиці типовий порядок "Прізвище Ім'я", у латиниці "Ім'я Прізвище".
            if any('\u0400' <= ch <= '\u04FF' for ch in name):
                last, first = parts[0], parts[1]
            else:
                first, last = parts[0], parts[-1]

    conn.execute(
        'INSERT INTO donor (donor_id,donor_type,first_name,last_name,org_name,'
        'display_name,name_normalized,country,city,donor_status,created_at,'
        'updated_at,created_by,data_quality_flags) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (donor_id, 'organization' if org else 'individual', first, last,
         name if org else None, name or 'Без імені', name_norm,
         rec.get('country'), rec.get('city'), 'active', now(), now(),
         'pipeline', json.dumps(flags, ensure_ascii=False) if flags else None))

    matcher.register_donor(donor_id, name_norm, rec.get('city'), rec.get('country'))
    add_identifiers(conn, matcher, donor_id, rec)
    write_consent(conn, donor_id, rec)
    return donor_id


def add_identifiers(conn, matcher: DonorMatcher, donor_id: str, rec: dict):
    """
    Ідентифікатори накопичуються.

    Це і є механізм, завдяки якому база з часом стає розумнішою:
    донор, вперше зафіксований з банківського переказу (тільки ім'я
    та IBAN), після пожертви через сайт отримує email — і всі наступні
    його пожертви з будь-якого каналу знаходяться миттєво й точно.
    """
    pairs = [
        ('email', rec.get('email'), normalize_email),
        ('phone', rec.get('phone'), normalize_phone),
        ('paypal_payer_id', rec.get('processor_id'), lambda v: v.lower()),
        ('bank_account_hash', rec.get('bank_account_hash'), lambda v: v),
    ]
    for id_type, raw, norm_fn in pairs:
        if not raw:
            continue
        value = norm_fn(raw)
        if not value:
            continue
        existing = conn.execute(
            'SELECT identifier_id FROM donor_identifier WHERE donor_id=? '
            'AND id_type=? AND id_value_normalized=?',
            (donor_id, id_type, value)).fetchone()
        if existing:
            conn.execute('UPDATE donor_identifier SET last_seen_at=? WHERE identifier_id=?',
                         (now(), existing['identifier_id']))
            continue
        conn.execute(
            'INSERT INTO donor_identifier (identifier_id,donor_id,id_type,id_value,'
            'id_value_normalized,is_primary,is_verified,first_seen_at,last_seen_at) '
            'VALUES (?,?,?,?,?,?,?,?,?)',
            (new_id('idf'), donor_id, id_type, str(raw), value,
             1 if id_type == 'email' else 0, 0, now(), now()))
        matcher.register_identifier(id_type, value, donor_id)


def write_consent(conn, donor_id: str, rec: dict):
    """
    Згода фіксується лише тоді, коли її справді питали.

    Тиша не є згодою. Пожертва через банк не дає права на розсилку,
    і "never_asked" тут — не заглушка, а юридично значущий стан.
    """
    if not rec.get('consent_known'):
        exists = conn.execute(
            'SELECT 1 FROM donor_consent WHERE donor_id=? AND consent_type=?',
            (donor_id, 'email_marketing')).fetchone()
        if not exists:
            conn.execute(
                'INSERT INTO donor_consent (consent_id,donor_id,consent_type,status,'
                'legal_basis,consent_source) VALUES (?,?,?,?,?,?)',
                (new_id('cns'), donor_id, 'email_marketing', 'never_asked',
                 'legitimate_interest', 'implicit'))
        return

    status = 'granted' if rec.get('consent') else 'revoked'
    row = conn.execute(
        'SELECT consent_id,status FROM donor_consent WHERE donor_id=? AND consent_type=?',
        (donor_id, 'email_marketing')).fetchone()
    if row:
        if row['status'] != status:
            conn.execute(
                'UPDATE donor_consent SET status=?, granted_at=?, revoked_at=? '
                'WHERE consent_id=?',
                (status, now() if status == 'granted' else None,
                 now() if status == 'revoked' else None, row['consent_id']))
    else:
        conn.execute(
            'INSERT INTO donor_consent (consent_id,donor_id,consent_type,status,'
            'legal_basis,granted_at,revoked_at,consent_source) VALUES (?,?,?,?,?,?,?,?)',
            (new_id('cns'), donor_id, 'email_marketing', status, 'consent',
             now() if status == 'granted' else None,
             now() if status == 'revoked' else None, 'website_form'))


def update_donor_profile(conn, matcher: DonorMatcher, donor_id: str, rec: dict):
    """
    Збагачення профілю новими даними без затирання наявних.

    Правило: порожнє поле заповнюємо, непорожнє не чіпаємо автоматично.
    Автоматична зміна вже відомої адреси чи імені — типовий спосіб
    зіпсувати базу свіжим сміттям із гіршого за якістю каналу.
    """
    row = conn.execute('SELECT * FROM donor WHERE donor_id=?', (donor_id,)).fetchone()
    updates = {}
    for field, value in (('country', rec.get('country')), ('city', rec.get('city'))):
        if value and not row[field]:
            updates[field] = value
    if updates:
        sets = ', '.join(f'{k}=?' for k in updates)
        conn.execute(f'UPDATE donor SET {sets}, updated_at=? WHERE donor_id=?',
                     (*updates.values(), now(), donor_id))
    add_identifiers(conn, matcher, donor_id, rec)
    write_consent(conn, donor_id, rec)


# ==========================================================================
# ПІДПИСКИ
# ==========================================================================

def ensure_plan(conn, donor_id: str, rec: dict, source_id: str) -> str | None:
    sub = rec.get('subscription_id')
    if not sub:
        return None
    row = conn.execute(
        'SELECT plan_id FROM recurring_plan WHERE external_subscription_id=?',
        (sub,)).fetchone()
    if row:
        plan_id = row['plan_id']
    else:
        plan_id = new_id('plan')
        conn.execute(
            'INSERT INTO recurring_plan (plan_id,donor_id,source_id,'
            'external_subscription_id,amount,currency,frequency,started_on,status) '
            'VALUES (?,?,?,?,?,?,?,?,?)',
            (plan_id, donor_id, source_id, sub, rec['amount'], rec['currency'],
             'monthly', rec['occurred_at'].date().isoformat(), 'active'))

    # Невдалий платіж не скасовує підписку, але переводить її у стан,
    # який має запускати dunning-процес.
    if rec.get('status') == 'failed':
        conn.execute(
            "UPDATE recurring_plan SET failed_attempts=failed_attempts+1, "
            "status='failing' WHERE plan_id=?", (plan_id,))
    elif rec.get('status') == 'completed':
        conn.execute(
            "UPDATE recurring_plan SET failed_attempts=0, status='active' "
            "WHERE plan_id=? AND status='failing'", (plan_id,))
    return plan_id


# ==========================================================================
# ПАЙПЛАЙН
# ==========================================================================

def file_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            h.update(chunk)
    return h.hexdigest()


def ingest_file(conn, matcher: DonorMatcher, filename: str, stats: dict):
    source_id, parser = CHANNELS[filename]
    path = os.path.join(INCOMING, filename)
    fhash = file_hash(path)

    # --- Захист від повторного завантаження файлу -----------------------
    dup = conn.execute(
        'SELECT batch_id, file_name FROM ingestion_batch WHERE file_hash=?',
        (fhash,)).fetchone()
    if dup:
        batch_id = new_id('batch')
        conn.execute(
            'INSERT INTO ingestion_batch (batch_id,source_id,file_name,file_hash,'
            'received_at,status,error_message) VALUES (?,?,?,?,?,?,?)',
            (batch_id, source_id, filename, fhash + '_dup', now(), 'failed',
             f'Ідентичний вміст вже завантажено у {dup["file_name"]}'))
        stats['files_skipped'].append((filename, dup['file_name']))
        return

    with open(path, encoding='utf-8') as f:
        rows = list(csv.DictReader(f))

    batch_id = new_id('batch')
    conn.execute(
        'INSERT INTO ingestion_batch (batch_id,source_id,file_name,file_hash,'
        'received_at,declared_row_count,status) VALUES (?,?,?,?,?,?,?)',
        (batch_id, source_id, filename, fhash, now(), len(rows), 'parsed'))

    for row in rows:
        process_row(conn, matcher, row, batch_id, source_id, parser, stats)

    conn.execute("UPDATE ingestion_batch SET status='loaded' WHERE batch_id=?", (batch_id,))


def process_row(conn, matcher, row, batch_id, source_id, parser, stats):
    payload = json.dumps(row, ensure_ascii=False)
    row_hash = hashlib.sha256(payload.encode()).hexdigest()
    rec = parser(row)
    ext = rec.get('external_id')

    # --- Ідемпотентність на рівні рядка ---------------------------------
    # Унікальний індекс у raw_donation ловить ретрай вебхука
    # та повторний рядок у виписці.
    raw_id = new_id('raw')
    try:
        conn.execute(
            'INSERT INTO raw_donation (raw_id,batch_id,source_id,external_id,'
            'payload,row_hash,received_at,processing_status) VALUES (?,?,?,?,?,?,?,?)',
            (raw_id, batch_id, source_id, ext or None, payload, row_hash,
             now(), 'pending'))
    except sqlite3.IntegrityError:
        # Спрацював унікальний ключ (source, external_id): та сама
        # транзакція вже є. Ловимо саме IntegrityError, а не будь-який
        # виняток — інакше реальний збій бази тихо порахувався б
        # як відсічений дублікат і зник би зі статистики.
        stats['duplicates'].append((source_id, ext))
        return

    # --- Валідація ------------------------------------------------------
    errors = validate(rec)
    if errors:
        conn.execute(
            "UPDATE raw_donation SET processing_status='rejected', processed_at=?, "
            "reject_reason=? WHERE raw_id=?", (now(), ','.join(errors), raw_id))
        for e in errors:
            log_issue(conn, 'raw_donation', raw_id, e, 'high', payload[:300])
        # Гроші прийшли, але запис непридатний — це не привід забути про нього.
        conn.execute(
            'INSERT INTO match_review_queue (review_id,raw_id,candidates,top_score,'
            'reason,status,created_at) VALUES (?,?,?,?,?,?,?)',
            (new_id('rev'), raw_id, '[]', None, 'incomplete_data', 'open', now()))
        stats['rejected'].append((ext, errors))
        return

    for w in soft_warnings(rec):
        log_issue(conn, 'raw_donation', raw_id, w, 'low', ext or '')

    # --- Анонімна пожертва ----------------------------------------------
    if rec.get('is_anonymous'):
        ensure_anonymous_donor(conn)
        donor_id, method, conf, outcome = ANONYMOUS_DONOR_ID, 'manual', 1.0, 'auto'
        stats['anonymous'] += 1
    else:
        # --- Матчинг -----------------------------------------------------
        result = matcher.match(rec)
        outcome, method, conf = result.outcome, result.method, result.confidence

        if outcome in ('auto', 'provisional'):
            donor_id = result.donor_id
            update_donor_profile(conn, matcher, donor_id, rec)
        else:
            flags = soft_warnings(rec)
            donor_id = create_donor(conn, matcher, rec, flags)
            if outcome == 'new':
                method = 'new_donor'

        if outcome in ('provisional', 'ambiguous'):
            conn.execute(
                'INSERT INTO match_review_queue (review_id,raw_id,candidates,'
                'top_score,reason,status,created_at) VALUES (?,?,?,?,?,?,?)',
                (new_id('rev'), raw_id, candidates_json(result.candidates),
                 conf, 'ambiguous_match' if outcome == 'ambiguous' else 'provisional_link',
                 'open', now()))
        stats['outcomes'][outcome] = stats['outcomes'].get(outcome, 0) + 1
        stats['decisions'].append(dict(external_id=ext, donor_id=donor_id,
                                       outcome=outcome, method=method,
                                       confidence=conf, reason=result.reason))

    # --- Гроші ----------------------------------------------------------
    date_str = rec['occurred_at'].date().isoformat()
    rate = get_fx_rate(conn, rec['currency'], date_str)
    amount_base = round(rec['amount'] * rate, 2)

    fee = rec.get('fee')
    if fee is None:
        src = conn.execute('SELECT fee_percent,fee_fixed FROM source WHERE source_id=?',
                           (source_id,)).fetchone()
        fee = rec['amount'] * (src['fee_percent'] or 0) / 100 + (src['fee_fixed'] or 0)
    fee_base = round(fee * rate, 2)

    plan_id = ensure_plan(conn, donor_id, rec, source_id)

    refund_of = None
    if rec.get('refund_of'):
        ref = conn.execute(
            'SELECT donation_id FROM donation WHERE source_id=? AND external_transaction_id=?',
            (source_id, rec['refund_of'])).fetchone()
        if ref:
            refund_of = ref['donation_id']
            conn.execute("UPDATE donation SET donation_status='refunded' "
                         'WHERE donation_id=?', (refund_of,))

    conn.execute(
        'INSERT INTO donation (donation_id,donor_id,raw_id,source_id,campaign_id,'
        'fund_id,plan_id,external_transaction_id,donated_at,amount_original,currency,'
        'fx_rate,amount_base,fee_base,amount_net_base,payment_method,donation_status,'
        'refund_of_donation_id,is_recurring,is_anonymous,match_method,match_confidence,'
        'created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (new_id('don'), donor_id, raw_id, source_id, rec.get('campaign'),
         rec.get('fund'), plan_id, ext, rec['occurred_at'].isoformat(),
         rec['amount'], rec['currency'], rate, amount_base, fee_base,
         round(amount_base - fee_base, 2), rec.get('payment_method'),
         rec.get('status', 'completed'), refund_of,
         1 if rec.get('is_recurring') else 0, 1 if rec.get('is_anonymous') else 0,
         method, conf, now()))

    conn.execute("UPDATE raw_donation SET processing_status='processed', "
                 'processed_at=? WHERE raw_id=?', (now(), raw_id))
    stats['loaded'] += 1


def assign_sequences(conn):
    """
    Порядковий номер пожертви кожного донора.

    Рахуємо після завантаження всіх каналів: файли приходять у
    довільному порядку, і "перша пожертва" визначається датою
    транзакції, а не моментом імпорту.
    """
    rows = conn.execute(
        "SELECT donation_id, donor_id FROM donation WHERE donation_status="
        "'completed' ORDER BY donor_id, donated_at").fetchall()
    seq, prev = 0, None
    for r in rows:
        seq = seq + 1 if r['donor_id'] == prev else 1
        prev = r['donor_id']
        conn.execute('UPDATE donation SET donation_sequence=? WHERE donation_id=?',
                     (seq, r['donation_id']))


def run(fresh: bool = True) -> dict:
    conn = connect(fresh=fresh)
    matcher = DonorMatcher(conn)
    stats = dict(loaded=0, anonymous=0, duplicates=[], rejected=[],
                 files_skipped=[], outcomes={}, decisions=[])

    run_id = new_id('run')
    conn.execute(
        'INSERT INTO automation_run (run_id,job_name,started_at,status) '
        "VALUES (?,?,?,?)", (run_id, 'ingest_donations', now(), 'running'))

    # Порядок навмисний: спершу найякісніше джерело.
    # Донор, у якого вже є email із сайту, потім знаходиться з банківської
    # виписки за іменем із набагато більшою впевненістю.
    order = ['website_donations.csv', 'paypal_transactions.csv',
             'paypal_transactions_REIMPORT.csv', 'bank_statement.csv',
             'checks_manual.csv']
    for filename in order:
        ingest_file(conn, matcher, filename, stats)

    assign_sequences(conn)

    review = conn.execute(
        "SELECT COUNT(*) c FROM match_review_queue WHERE status='open'").fetchone()['c']
    conn.execute(
        'UPDATE automation_run SET finished_at=?, status=?, records_in=?, '
        'records_ok=?, records_review=?, records_failed=? WHERE run_id=?',
        (now(), 'success', stats['loaded'] + len(stats['rejected']) + len(stats['duplicates']),
         stats['loaded'], review, len(stats['rejected']), run_id))
    conn.commit()
    stats['conn'] = conn
    return stats
