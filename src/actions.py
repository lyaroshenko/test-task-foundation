"""
Автоматичні дії після отримання пожертви.

Правила навмисно тримаються тут, а не всередині пайплайна обробки.
Фандрейзинг-команда змінює логіку комунікацій постійно, а логіку
матчингу — раз на рік. Змішувати їх в одному місці означає
ризикувати даними щоразу, коли хтось хоче поправити текст листа.

Головний принцип: жодна дія не виконується всліпу. Кожна або
створює запис у communication зі статусом 'queued', або фіксує
'skipped' з причиною. Порожній результат теж має бути пояснений.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(__file__))
from db import new_id, now
from metrics import REFERENCE_DATE

# Транзакційні комунікації: підтвердження й подяка за конкретну
# пожертву. Згода на маркетинг для них не потрібна — це відповідь
# на дію самої людини, а не розсилка. Юридична підстава — legitimate
# interest. Усе інше без opt-in не відправляється.
TRANSACTIONAL = {'receipt', 'thank_you'}

THANK_YOU_SLA_HOURS = 48
MAJOR_GIFT_THRESHOLD = 25_000     # UAH
UPGRADE_MIN_GIFTS = 3             # разових пожертв за рік для пропозиції підписки


def has_consent(conn, donor_id: str) -> bool:
    row = conn.execute(
        "SELECT status FROM donor_consent WHERE donor_id=? AND consent_type='email_marketing'",
        (donor_id,)).fetchone()
    return bool(row) and row['status'] == 'granted'


def has_email(conn, donor_id: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM donor_identifier WHERE donor_id=? AND id_type='email' LIMIT 1",
        (donor_id,)).fetchone())


def _queue(conn, donor_id, comm_type, channel, donation_id=None,
           template=None, automated=True, scheduled=None):
    conn.execute(
        'INSERT INTO communication (communication_id,donor_id,donation_id,comm_type,'
        'channel,direction,scheduled_at,status,template_code,is_automated) '
        "VALUES (?,?,?,?,?,'outbound',?,'queued',?,?)",
        (new_id('cm'), donor_id, donation_id, comm_type, channel,
         scheduled or now(), template, 1 if automated else 0))


def _skip(conn, donor_id, comm_type, reason, donation_id=None):
    conn.execute(
        'INSERT INTO communication (communication_id,donor_id,donation_id,comm_type,'
        "channel,direction,status,skip_reason,is_automated) "
        "VALUES (?,?,?,?,'email','outbound','skipped',?,1)",
        (new_id('cm'), donor_id, donation_id, comm_type, reason))


def run_actions(conn) -> dict:
    """
    Проходить по пожертвах, для яких ще не створено комунікацій,
    і застосовує правила. Ідемпотентно: повторний запуск не
    продублює подяку.
    """
    stats = {}

    def bump(key):
        stats[key] = stats.get(key, 0) + 1

    donations = conn.execute("""
        SELECT dn.donation_id, dn.donor_id, dn.donated_at, dn.amount_base,
               dn.donation_sequence, dn.is_anonymous, dn.is_recurring,
               d.donor_type, m.segment, m.donation_count
        FROM donation dn
        JOIN donor d  ON d.donor_id = dn.donor_id
        LEFT JOIN donor_metrics m ON m.donor_id = dn.donor_id
        WHERE dn.donation_status = 'completed'
          AND NOT EXISTS (SELECT 1 FROM communication c
                          WHERE c.donation_id = dn.donation_id)
        ORDER BY dn.donated_at
    """).fetchall()

    for d in donations:
        donor_id, did = d['donor_id'], d['donation_id']

        # --- Анонім: жодних комунікацій, тільки фінансовий облік ---------
        if d['is_anonymous'] or d['donor_type'] == 'anonymous':
            _skip(conn, donor_id, 'thank_you', 'анонімна пожертва', did)
            bump('skipped_anonymous')
            continue

        # --- Немає email: подяка неможлива, але й губити не можна --------
        if not has_email(conn, donor_id):
            _skip(conn, donor_id, 'thank_you', 'немає email — потрібен інший канал', did)
            bump('skipped_no_email')
            # Пожертва без контактів — привід поставити задачу на збагачення
            # профілю, а не мовчки залишити донора без подяки назавжди.
            _queue(conn, donor_id, 'call', 'phone', did,
                   template='enrich_contact', automated=False)
            bump('task_enrich_contact')
            continue

        # --- Великий донат: автоматика вимикається ----------------------
        # Стандартний шаблонний лист на пожертву в 250 000 грн —
        # найдешевший спосіб втратити великого донора.
        if d['amount_base'] >= MAJOR_GIFT_THRESHOLD:
            _skip(conn, donor_id, 'thank_you',
                  'великий донат — персональний контакт', did)
            _queue(conn, donor_id, 'call', 'phone', did,
                   template='major_gift_personal_call', automated=False)
            bump('task_major_gift')
            continue

        # --- Квитанція та подяка (транзакційні) -------------------------
        _queue(conn, donor_id, 'receipt', 'email', did, template='receipt_std')
        bump('receipt')

        sent_at = datetime.fromisoformat(d['donated_at'])
        _queue(conn, donor_id, 'thank_you', 'email', did, template='thank_you_std',
               scheduled=(sent_at + timedelta(hours=2)).isoformat())
        bump('thank_you')

        # --- Далі — тільки за наявності згоди ---------------------------
        if not has_consent(conn, donor_id):
            _skip(conn, donor_id, 'welcome', 'немає згоди на маркетинг', did)
            bump('skipped_no_consent')
            continue

        segment = d['segment']
        if d['donation_sequence'] == 1:
            _queue(conn, donor_id, 'welcome', 'email', did, template='welcome_series_1',
                   scheduled=(sent_at + timedelta(days=3)).isoformat())
            bump('welcome')
        elif segment == 'reactivated':
            # Повернутому донору не можна слати welcome: він у нас не новий,
            # і лист "раді знайомству" виглядає як доказ, що його забули.
            _queue(conn, donor_id, 'reactivation', 'email', did,
                   template='welcome_back', scheduled=(sent_at + timedelta(days=2)).isoformat())
            bump('reactivation')

    # --- Пропозиція перейти на регулярну підтримку ----------------------
    upgrade = conn.execute("""
        SELECT m.donor_id
        FROM donor_metrics m
        WHERE m.has_active_recurring = 0
          AND m.donation_count >= ?
          AND m.days_since_last_gift <= 365
          AND m.segment IN ('active','new')
          AND NOT EXISTS (SELECT 1 FROM communication c
                          WHERE c.donor_id = m.donor_id AND c.comm_type = 'ask'
                            AND c.template_code = 'recurring_upgrade')
    """, (UPGRADE_MIN_GIFTS,)).fetchall()
    for r in upgrade:
        if has_consent(conn, r['donor_id']) and has_email(conn, r['donor_id']):
            _queue(conn, r['donor_id'], 'ask', 'email', template='recurring_upgrade')
            bump('recurring_upgrade_ask')

    # --- Dunning: підписка з невдалим платежем --------------------------
    failing = conn.execute(
        "SELECT donor_id, plan_id FROM recurring_plan WHERE status='failing'").fetchall()
    for r in failing:
        exists = conn.execute(
            "SELECT 1 FROM communication WHERE donor_id=? AND comm_type='recurring_dunning' "
            "AND status='queued'", (r['donor_id'],)).fetchone()
        if not exists:
            _queue(conn, r['donor_id'], 'recurring_dunning', 'email',
                   template='card_update_request')
            bump('dunning')

    # --- Реактиваційна кампанія для сплячих -----------------------------
    lapsing = conn.execute("""
        SELECT m.donor_id FROM donor_metrics m
        WHERE m.segment = 'lapsing'
          AND NOT EXISTS (SELECT 1 FROM communication c
                          WHERE c.donor_id = m.donor_id
                            AND c.comm_type = 'reactivation' AND c.status <> 'skipped')
    """).fetchall()
    for r in lapsing:
        if has_consent(conn, r['donor_id']) and has_email(conn, r['donor_id']):
            _queue(conn, r['donor_id'], 'reactivation', 'email',
                   template='we_miss_you')
            bump('lapsing_campaign')
        else:
            _skip(conn, r['donor_id'], 'reactivation', 'немає згоди або email')
            bump('skipped_no_consent')

    conn.commit()
    return stats


def followup_list(conn, limit: int = 15) -> list:
    """
    Список донорів для персонального follow-up.

    Пріоритет — не за розміром пожертви, а за співвідношенням
    цінності донора й ризику його втратити. Донор, який давав багато
    і давно замовк, важливіший за того, хто щойно дав уперше.
    """
    return conn.execute("""
        SELECT d.display_name,
               m.segment,
               m.donation_count                        AS gifts,
               ROUND(m.lifetime_amount_base)           AS lifetime,
               m.days_since_last_gift                  AS days_quiet,
               CASE WHEN dc.status = 'granted' THEN 'так' ELSE 'ні' END AS consent,
               ROUND(
                   m.lifetime_amount_base / 1000.0
                   * (m.days_since_last_gift / 90.0)
                   * CASE WHEN m.donation_count > 1 THEN 1.5 ELSE 1.0 END
               , 1) AS priority_score
        FROM donor_metrics m
        JOIN donor d ON d.donor_id = m.donor_id
        LEFT JOIN donor_consent dc
               ON dc.donor_id = m.donor_id AND dc.consent_type = 'email_marketing'
        WHERE m.segment IN ('major','lapsing','lapsed','recurring_at_risk','reactivated')
          AND d.merged_into_donor_id IS NULL
        ORDER BY priority_score DESC
        LIMIT ?
    """, (limit,)).fetchall()
