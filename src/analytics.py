"""
Fundraising-аналітика.

Сім показників, підібраних за принципом: кожен має відповідати на
питання, після якого хтось щось робить інакше. Метрика, яку приємно
показати на раді, але за якою неможливо ухвалити рішення, у звіт
не потрапляє.

Три групи:
  ОБСЯГ      — скільки зібрали і від кого
  ЗДОРОВ'Я   — що відбувається з базою донорів (тут живуть ризики)
  ОПЕРАЦІЙНА — чи працює сам процес
"""

from __future__ import annotations

import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(__file__))
from metrics import REFERENCE_DATE

CURRENT_YEAR = REFERENCE_DATE.year
PREVIOUS_YEAR = CURRENT_YEAR - 1


# ==========================================================================
# 1. ОБСЯГ ЗБОРУ ТА КІЛЬКІСТЬ ДОНОРІВ
# ==========================================================================

def total_raised(conn) -> list[dict]:
    """
    Скільки зібрано, скільки дійшло, скільки донорів.

    Показує: базовий обсяг у розрізі років.
    Навіщо: gross і net розходяться на комісіях платіжних систем.
    Планувати бюджет програм можна тільки за net.
    Рішення: якщо розрив між gross і net росте, варто переглянути
    мікс каналів — банківський переказ дешевший за PayPal.
    """
    return [dict(r) for r in conn.execute("""
        SELECT strftime('%Y', donated_at)             AS year,
               COUNT(*)                               AS gifts,
               COUNT(DISTINCT donor_id)               AS donors,
               ROUND(SUM(amount_base))                AS gross,
               ROUND(SUM(amount_net_base))            AS net,
               ROUND(SUM(fee_base))                   AS fees
        FROM donation
        WHERE donation_status = 'completed'
        GROUP BY year ORDER BY year
    """)]


# ==========================================================================
# 2. НОВІ ПРОТИ ПОВТОРНИХ
# ==========================================================================

def new_vs_returning(conn) -> list[dict]:
    """
    Розподіл доходу між першими та повторними пожертвами.

    Показує: за чий рахунок живе організація.
    Навіщо: зростання лише за рахунок нових донорів — дороге і крихке.
    Залучення нового коштує в рази більше, ніж утримання наявного.
    Рішення: якщо частка повторних падає, ресурс треба перекинути
    з реклами на роботу з наявною базою.
    """
    return [dict(r) for r in conn.execute("""
        SELECT strftime('%Y', donated_at) AS year,
               SUM(CASE WHEN donation_sequence = 1 THEN 1 ELSE 0 END)           AS new_gifts,
               SUM(CASE WHEN donation_sequence > 1 THEN 1 ELSE 0 END)           AS repeat_gifts,
               ROUND(SUM(CASE WHEN donation_sequence = 1 THEN amount_base ELSE 0 END)) AS new_amount,
               ROUND(SUM(CASE WHEN donation_sequence > 1 THEN amount_base ELSE 0 END)) AS repeat_amount
        FROM donation
        WHERE donation_status = 'completed' AND donation_sequence IS NOT NULL
        GROUP BY year ORDER BY year
    """)]


# ==========================================================================
# 3. УТРИМАННЯ ДОНОРІВ
# ==========================================================================

def retention(conn, year: int = PREVIOUS_YEAR) -> dict:
    """
    Скільки донорів минулого року дали й цього року.

    Показує: чи повертаються люди.
    Навіщо: це головний показник здоров'я фандрейзингу. Він єдиний
    передбачає майбутнє: обсяг збору говорить про те, що вже сталося,
    утримання — про те, що станеться наступного року.
    Окремо рахуємо утримання новачків: у секторі воно традиційно
    втричі нижче за загальне, і саме там ховається найбільша втрата.
    Рішення: падіння утримання новачків означає проблему з першими
    90 днями — подякою, welcome-серією, звітом про використання коштів.
    """
    def donors_in(y, first_time_only=False):
        cond = 'AND donation_sequence = 1' if first_time_only else ''
        return {r['donor_id'] for r in conn.execute(
            f"SELECT DISTINCT donor_id FROM donation WHERE donation_status='completed' "
            f"AND strftime('%Y', donated_at)=? {cond}", (str(y),))}

    base = donors_in(year)
    nxt = donors_in(year + 1)
    first_timers = donors_in(year, first_time_only=True)

    retained = base & nxt
    ft_retained = first_timers & nxt

    return dict(
        year=year,
        base_donors=len(base),
        retained=len(retained),
        rate=round(len(retained) / len(base) * 100, 1) if base else 0.0,
        first_time_donors=len(first_timers),
        first_time_retained=len(ft_retained),
        first_time_rate=round(len(ft_retained) / len(first_timers) * 100, 1)
                        if first_timers else 0.0,
    )


# ==========================================================================
# 4. ЧАСТКА РЕГУЛЯРНИХ ПОЖЕРТВ
# ==========================================================================

def recurring_share(conn) -> dict:
    """
    Яка частина доходу приходить за підпискою.

    Показує: наскільки передбачуваний дохід.
    Навіщо: регулярні донори дають менші суми, але лишаються в рази
    довше. Це та частина бюджету, під яку можна планувати зарплати
    й багаторічні програми, а не разові акції.
    Рішення: низька частка — привід запускати кампанію переходу
    на підписку серед донорів з 3+ разовими пожертвами.
    """
    row = conn.execute("""
        SELECT ROUND(SUM(CASE WHEN is_recurring=1 THEN amount_base ELSE 0 END)) AS rec,
               ROUND(SUM(amount_base))                                          AS total
        FROM donation WHERE donation_status='completed'
    """).fetchone()
    plans = conn.execute(
        "SELECT status, COUNT(*) c FROM recurring_plan GROUP BY status").fetchall()
    total = row['total'] or 1
    return dict(recurring_amount=row['rec'] or 0, total_amount=row['total'] or 0,
                share_pct=round((row['rec'] or 0) / total * 100, 1),
                plans={r['status']: r['c'] for r in plans})


# ==========================================================================
# 5. СЕРЕДНІЙ І МЕДІАННИЙ ЧЕК
# ==========================================================================

def gift_size(conn) -> dict:
    """
    Середня і медіанна пожертва.

    Показує: типовий розмір внеску.
    Навіщо: обидва числа потрібні саме разом. Один великий донат
    підіймає середнє так, що воно перестає описувати реальність.
    Медіана показує, скільки дає звичайна людина, і саме на неї
    треба орієнтувати суми в формі на сайті.
    Рішення: великий розрив між середнім і медіаною означає
    залежність від кількох великих донорів — це ризик, а не успіх.
    """
    amounts = [r['amount_base'] for r in conn.execute(
        "SELECT amount_base FROM donation WHERE donation_status='completed' "
        "AND amount_base > 0")]
    if not amounts:
        return {}
    return dict(count=len(amounts),
                mean=round(statistics.mean(amounts)),
                median=round(statistics.median(amounts)),
                p90=round(sorted(amounts)[int(len(amounts) * 0.9)]),
                max=round(max(amounts)))


# ==========================================================================
# 6. КОНЦЕНТРАЦІЯ ДОХОДУ
# ==========================================================================

def concentration(conn) -> dict:
    """
    Яку частку збору дають топ-10% донорів.

    Показує: наскільки організація залежить від кількох людей.
    Навіщо: це метрика ризику, а не успіху. Якщо 3 донори дають
    половину бюджету, втрата одного з них — криза, і план Б
    має існувати до того, як вона настане.
    Рішення: висока концентрація означає дві паралельні задачі —
    персональна робота з великими донорами та розширення масової бази.
    """
    rows = sorted((r['lifetime_amount_base'] for r in conn.execute(
        'SELECT lifetime_amount_base FROM donor_metrics')), reverse=True)
    if not rows:
        return {}
    total = sum(rows)
    top10_n = max(1, len(rows) // 10)
    return dict(donors=len(rows), total=round(total),
                top10_donors=top10_n,
                top10_amount=round(sum(rows[:top10_n])),
                top10_share=round(sum(rows[:top10_n]) / total * 100, 1),
                top1_share=round(rows[0] / total * 100, 1))


# ==========================================================================
# 7. ОПЕРАЦІЙНА: ШВИДКІСТЬ ПОДЯКИ ТА ЯКІСТЬ ДАНИХ
# ==========================================================================

def operations(conn) -> dict:
    """
    Чи працює сам процес.

    Показує: частку пожертв, за які подякували вчасно, обсяг ручної
    черги та кількість відкритих проблем якості даних.
    Навіщо: класична фандрейзингова аналітика вимірює гроші й забуває
    процес. Але саме тут ховається причина падіння утримання:
    донор, якому не подякували, майже не повертається.
    Рішення: зростання черги на ручний розгляд означає, що
    автоматизація перестала справлятися і потрібне доналаштування
    порогів, а не наймання ще однієї людини на ручну обробку.
    """
    total = conn.execute(
        "SELECT COUNT(*) c FROM donation WHERE donation_status='completed' "
        "AND is_anonymous=0").fetchone()['c']
    thanked = conn.execute(
        "SELECT COUNT(DISTINCT donation_id) c FROM communication "
        "WHERE comm_type='thank_you' AND status<>'skipped'").fetchone()['c']
    skipped = conn.execute(
        "SELECT skip_reason, COUNT(*) c FROM communication WHERE status='skipped' "
        'GROUP BY skip_reason ORDER BY c DESC').fetchall()
    queue = conn.execute(
        "SELECT reason, COUNT(*) c FROM match_review_queue WHERE status='open' "
        'GROUP BY reason').fetchall()
    dq = conn.execute(
        'SELECT issue_type, severity, COUNT(*) c FROM data_quality_issue '
        'WHERE resolved_at IS NULL GROUP BY issue_type, severity ORDER BY c DESC'
    ).fetchall()
    consent = conn.execute("""
        SELECT SUM(CASE WHEN status='granted' THEN 1 ELSE 0 END) granted,
               COUNT(*) total FROM donor_consent WHERE consent_type='email_marketing'
    """).fetchone()

    return dict(
        donations=total, thanked=thanked,
        thanked_pct=round(thanked / total * 100, 1) if total else 0,
        skipped=[(r['skip_reason'], r['c']) for r in skipped],
        review_queue=[(r['reason'], r['c']) for r in queue],
        data_quality=[(r['issue_type'], r['severity'], r['c']) for r in dq],
        consent_granted=consent['granted'] or 0, consent_total=consent['total'] or 0,
        reachable_pct=round((consent['granted'] or 0) / (consent['total'] or 1) * 100, 1),
    )


# ==========================================================================
# ЗВІТ
# ==========================================================================

def _bar(pct: float, width: int = 24) -> str:
    filled = int(round(pct / 100 * width))
    return '█' * filled + '·' * (width - filled)


def report(conn):
    print()
    print('=' * 72)
    print('FUNDRAISING DASHBOARD'.center(72))
    print(f'станом на {REFERENCE_DATE:%d.%m.%Y}'.center(72))
    print('=' * 72)

    print('\n1. ОБСЯГ ЗБОРУ')
    print(f'   {"рік":<6}{"пожертв":>9}{"донорів":>10}{"gross, UAH":>15}'
          f'{"net, UAH":>14}{"комісії":>11}')
    for r in total_raised(conn):
        print(f'   {r["year"]:<6}{r["gifts"]:>9}{r["donors"]:>10}'
              f'{r["gross"]:>15,.0f}{r["net"]:>14,.0f}{r["fees"]:>11,.0f}')

    print('\n2. НОВІ ПРОТИ ПОВТОРНИХ (за сумою)')
    for r in new_vs_returning(conn):
        tot = (r['new_amount'] or 0) + (r['repeat_amount'] or 0)
        pct = (r['repeat_amount'] or 0) / tot * 100 if tot else 0
        print(f'   {r["year"]}  повторні {pct:>5.1f}%  {_bar(pct)}  '
              f'нові {r["new_gifts"]:>3} / повторні {r["repeat_gifts"]:>3}')

    print('\n3. УТРИМАННЯ ДОНОРІВ')
    for y in (CURRENT_YEAR - 2, PREVIOUS_YEAR):
        r = retention(conn, y)
        if not r['base_donors']:
            continue
        print(f'   {y} -> {y+1}:  загальне {r["rate"]:>5.1f}%  '
              f'({r["retained"]}/{r["base_donors"]})   '
              f'новачки {r["first_time_rate"]:>5.1f}%  '
              f'({r["first_time_retained"]}/{r["first_time_donors"]})')

    print('\n4. РЕГУЛЯРНІ ПОЖЕРТВИ')
    rs = recurring_share(conn)
    print(f'   частка доходу: {rs["share_pct"]}%  {_bar(rs["share_pct"])}')
    print(f'   {rs["recurring_amount"]:,.0f} з {rs["total_amount"]:,.0f} UAH')
    print(f'   підписки: {rs["plans"]}')

    print('\n5. РОЗМІР ПОЖЕРТВИ')
    g = gift_size(conn)
    print(f'   середня {g["mean"]:,} UAH   медіана {g["median"]:,} UAH   '
          f'p90 {g["p90"]:,}   макс {g["max"]:,}')
    ratio = g['mean'] / g['median'] if g['median'] else 0
    print(f'   середня/медіана = {ratio:.1f}x'
          f'{"  <- сильний перекіс у бік великих донатів" if ratio > 2 else ""}')

    print('\n6. КОНЦЕНТРАЦІЯ ДОХОДУ')
    c = concentration(conn)
    print(f'   топ-10% ({c["top10_donors"]} донорів) дають {c["top10_share"]}%  '
          f'{_bar(c["top10_share"])}')
    print(f'   найбільший донор один дає {c["top1_share"]}%')

    print('\n7. ОПЕРАЦІЙНІ ПОКАЗНИКИ')
    o = operations(conn)
    print(f'   подяку відправлено: {o["thanked_pct"]}% ({o["thanked"]}/{o["donations"]})')
    print(f'   доступні для комунікацій: {o["reachable_pct"]}% '
          f'({o["consent_granted"]}/{o["consent_total"]} мають opt-in)')
    print(f'   черга на ручний розгляд: {sum(c for _, c in o["review_queue"])}')
    for reason, cnt in o['review_queue']:
        print(f'      {reason:<24} {cnt}')
    print('   комунікації не відправлено:')
    for reason, cnt in o['skipped']:
        print(f'      {reason:<44} {cnt}')
    print('   відкриті проблеми якості даних:')
    for t, sev, cnt in o['data_quality'][:6]:
        print(f'      {t:<24} {sev:<8} {cnt}')
    print()
    print('=' * 72)
