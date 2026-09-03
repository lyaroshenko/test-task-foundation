"""
Розрахунок метрик донора та сегментація.

Метрики матеріалізуються в donor_metrics, а не рахуються запитом
щоразу. Причина практична: сегмент донора потрібен у момент
обробки пожертви, щоб вирішити, який лист відправити. Чекати на
важкий аналітичний запит у цій точці не можна.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))
from db import new_id, now

# Дата, відносно якої рахуються "днів тому". У проді це поточна дата,
# у прототипі фіксована, щоб результати були відтворюваними.
REFERENCE_DATE = datetime(2026, 9, 2)

# --------------------------------------------------------------------------
# Пороги сегментації
# --------------------------------------------------------------------------
# Значення підібрані під українську організацію середнього розміру.
# Вони мають бути параметром, а не константою в коді: у кожного фонду
# "великий донор" означає свою суму, і вона змінюється з роками.

MAJOR_LIFETIME_UAH = 50_000      # сумарно за весь час
MAJOR_SINGLE_UAH = 25_000        # або одна пожертва такого розміру
NEW_DONOR_DAYS = 90              # перша пожертва не давніша за
ACTIVE_DAYS = 365                # активним вважаємо, якщо давав за рік
LAPSING_DAYS = 730               # 12-24 міс — той, кого ще можна повернути
REACTIVATION_GAP_DAYS = 540      # пауза, після якої повернення = реактивація


def _quintile(value: float, sorted_values: list[float], reverse: bool = False) -> int:
    """Оцінка 1-5 за положенням у розподілі. 5 — найкраще."""
    if not sorted_values:
        return 3
    below = sum(1 for v in sorted_values if v < value)
    pct = below / len(sorted_values)
    score = min(5, int(pct * 5) + 1)
    return 6 - score if reverse else score


def recalculate_all(conn) -> dict:
    """
    Повний перерахунок метрик і сегментів.

    У продакшені викликається інкрементально для донорів, яких
    зачепила нова пачка даних. Повний перерахунок лишається
    як нічна звірка: він гарантує, що навіть якщо інкрементальне
    оновлення десь збилося, стан приходить до правильного.
    """
    rows = conn.execute("""
        SELECT d.donor_id,
               COUNT(*)                          AS cnt,
               MIN(dn.donated_at)                AS first_at,
               MAX(dn.donated_at)                AS last_at,
               SUM(dn.amount_base)               AS total,
               SUM(dn.amount_net_base)           AS total_net,
               MAX(dn.amount_base)               AS largest
        FROM donor d
        JOIN donation dn ON dn.donor_id = d.donor_id
        WHERE dn.donation_status = 'completed'
          AND d.merged_into_donor_id IS NULL
          AND d.donor_type <> 'anonymous'
        GROUP BY d.donor_id
    """).fetchall()

    if not rows:
        return dict(donors=0)

    # Розподіли для RFM рахуємо один раз по всій базі
    recencies, freqs, monetaries = [], [], []
    prepared = []
    for r in rows:
        last = datetime.fromisoformat(r['last_at'])
        first = datetime.fromisoformat(r['first_at'])
        days_since = (REFERENCE_DATE - last).days
        years = conn.execute(
            "SELECT COUNT(DISTINCT strftime('%Y', donated_at)) y FROM donation "
            "WHERE donor_id=? AND donation_status='completed'",
            (r['donor_id'],)).fetchone()['y']
        plan = conn.execute(
            "SELECT amount, currency, status FROM recurring_plan "
            "WHERE donor_id=? AND status IN ('active','failing') LIMIT 1",
            (r['donor_id'],)).fetchone()
        # Дата передостанньої пожертви. Потрібна саме вона, а не перша:
        # реактивація — це пауза ПЕРЕД останнім донатом, а не загальна
        # тривалість стосунків із донором.
        prev = conn.execute(
            "SELECT donated_at FROM donation WHERE donor_id=? "
            "AND donation_status='completed' ORDER BY donated_at DESC "
            "LIMIT 1 OFFSET 1", (r['donor_id'],)).fetchone()
        prepared.append(dict(
            donor_id=r['donor_id'], cnt=r['cnt'], first=first, last=last,
            total=r['total'] or 0, total_net=r['total_net'] or 0,
            largest=r['largest'] or 0, days_since=days_since, years=years,
            plan=plan,
            prev_gift=datetime.fromisoformat(prev['donated_at']) if prev else None))
        recencies.append(days_since)
        freqs.append(r['cnt'])
        monetaries.append(r['total'] or 0)

    recencies.sort(); freqs.sort(); monetaries.sort()

    changes = 0
    for p in prepared:
        segment = classify(p)

        prev = conn.execute(
            'SELECT segment FROM donor_metrics WHERE donor_id=?',
            (p['donor_id'],)).fetchone()
        prev_segment = prev['segment'] if prev else None

        conn.execute("""
            INSERT INTO donor_metrics (donor_id, first_donation_at, last_donation_at,
                donation_count, lifetime_amount_base, lifetime_net_base, avg_gift_base,
                largest_gift_base, days_since_last_gift, has_active_recurring,
                recurring_amount_base, distinct_years_given, consecutive_years_given,
                rfm_recency, rfm_frequency, rfm_monetary, segment, segment_changed_at,
                updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(donor_id) DO UPDATE SET
                first_donation_at=excluded.first_donation_at,
                last_donation_at=excluded.last_donation_at,
                donation_count=excluded.donation_count,
                lifetime_amount_base=excluded.lifetime_amount_base,
                lifetime_net_base=excluded.lifetime_net_base,
                avg_gift_base=excluded.avg_gift_base,
                largest_gift_base=excluded.largest_gift_base,
                days_since_last_gift=excluded.days_since_last_gift,
                has_active_recurring=excluded.has_active_recurring,
                recurring_amount_base=excluded.recurring_amount_base,
                distinct_years_given=excluded.distinct_years_given,
                rfm_recency=excluded.rfm_recency,
                rfm_frequency=excluded.rfm_frequency,
                rfm_monetary=excluded.rfm_monetary,
                segment=excluded.segment,
                updated_at=excluded.updated_at
        """, (
            p['donor_id'], p['first'].isoformat(), p['last'].isoformat(), p['cnt'],
            round(p['total'], 2), round(p['total_net'], 2),
            round(p['total'] / p['cnt'], 2), round(p['largest'], 2), p['days_since'],
            1 if p['plan'] else 0, p['plan']['amount'] if p['plan'] else 0,
            p['years'], p['years'],
            _quintile(p['days_since'], recencies, reverse=True),
            _quintile(p['cnt'], freqs), _quintile(p['total'], monetaries),
            segment, now(), now()))

        if prev_segment != segment:
            changes += 1
            conn.execute(
                'INSERT INTO donor_segment_history (history_id,donor_id,segment_from,'
                'segment_to,changed_at) VALUES (?,?,?,?,?)',
                (new_id('sgh'), p['donor_id'], prev_segment, segment, now()))

    # Статус донора в CRM тримаємо синхронним із сегментом
    conn.execute("""
        UPDATE donor SET donor_status='lapsed'
        WHERE donor_id IN (SELECT donor_id FROM donor_metrics WHERE segment='lapsed')
          AND donor_status='active'
    """)
    conn.commit()
    return dict(donors=len(prepared), segment_changes=changes)


def classify(p: dict) -> str:
    """
    Сегмент донора.

    Порядок перевірок важливіший за самі пороги: донор може підходити
    під кілька визначень одразу, і виграє те, яке диктує дію.
    Великий донор, який давно не давав, лишається великим — з ним
    працює менеджер, а не автоматична реактиваційна розсилка.
    """
    if p['total'] >= MAJOR_LIFETIME_UAH or p['largest'] >= MAJOR_SINGLE_UAH:
        return 'major'
    if p['plan'] and p['plan']['status'] == 'active':
        return 'recurring'
    if p['plan'] and p['plan']['status'] == 'failing':
        return 'recurring_at_risk'

    # Реактивований: повернувся після довгої паузи.
    #
    # Пауза рахується між останньою і ПЕРЕДостанньою пожертвою.
    # Проміжок від першої до останньої тут не годиться: донор, який
    # давав рівномірно чотири рази за три роки, має великий загальний
    # проміжок без жодної паузи — і отримав би лист "раді, що ви
    # повернулися", хоча нікуди не зникав.
    if p['days_since'] <= NEW_DONOR_DAYS and p.get('prev_gift'):
        gap = (p['last'] - p['prev_gift']).days
        if gap >= REACTIVATION_GAP_DAYS:
            return 'reactivated'

    if p['cnt'] == 1 and p['days_since'] <= NEW_DONOR_DAYS:
        return 'new'
    if p['days_since'] <= ACTIVE_DAYS:
        return 'active'
    if p['days_since'] <= LAPSING_DAYS:
        return 'lapsing'
    return 'lapsed'


def segment_summary(conn) -> list:
    return conn.execute("""
        SELECT segment,
               COUNT(*)                              AS donors,
               ROUND(SUM(lifetime_amount_base))      AS lifetime_value,
               ROUND(AVG(lifetime_amount_base))      AS avg_lifetime,
               ROUND(AVG(donation_count), 1)         AS avg_gifts
        FROM donor_metrics
        GROUP BY segment
        ORDER BY lifetime_value DESC
    """).fetchall()
