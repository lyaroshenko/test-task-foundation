"""
Звірка: чи збігається те, що в базі, з тим, що прийшло з каналу.

Це відповідь на пункт "контроль за роботою автоматизації".
Логи показують, що процес запустився. Звірка показує, що він
не загубив і не подвоїв гроші — а це різні питання.

Розходження очікується і не є помилкою саме тоді, коли ми знаємо
його причину: відхилені записи, дублікати, невдалі платежі.
Тривогу має викликати нез'ясована різниця.
"""

from __future__ import annotations

import csv
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from db import new_id, now
from ingest import CHANNELS, INCOMING, validate
from normalize import parse_amount


def reconcile(conn) -> list[dict]:
    results = []

    for filename, (source_id, parser) in CHANNELS.items():
        # Повторний імпорт свідомо не звіряємо: він і має бути відкинутий
        if 'REIMPORT' in filename:
            continue

        path = os.path.join(INCOMING, filename)
        with open(path, encoding='utf-8') as f:
            rows = list(csv.DictReader(f))

        # Контрольна сума з джерела: успішні надходження в оригінальній
        # валюті, як їх бачить сам канал.
        source_total, source_count = 0.0, 0
        seen, dup_total, dup_count = set(), 0.0, 0
        for row in rows:
            rec = parser(row)
            if rec.get('status') != 'completed' or rec.get('amount') is None:
                continue
            source_total += rec['amount']
            source_count += 1
            ext = rec.get('external_id')
            if ext and ext in seen:
                dup_count += 1
                dup_total += rec['amount']
            elif ext:
                seen.add(ext)

        db_row = conn.execute("""
            SELECT COUNT(*) c, COALESCE(SUM(amount_original),0) s
            FROM donation WHERE source_id=? AND donation_status='completed'
        """, (source_id,)).fetchone()

        # --- Коригування: різниці, причину яких ми знаємо ----------------
        # Звірка не має вимагати нульової різниці. Вона має вимагати,
        # щоб кожна гривня різниці була пояснена конкретною причиною.
        #
        # Відхилені записи коригують контрольну суму тільки тоді, коли
        # вони до неї входили. Рядок з нерозбірливою сумою у джерелі не
        # врахований, і "виправляти" на нього різницю було б подвійним
        # рахунком; рядок з нерозбірливою датою суму має, і врахувати
        # його необхідно.
        rej, rej_total = 0, 0.0
        for row in rows:
            rec = parser(row)
            if rec.get('status') != 'completed' or rec.get('amount') is None:
                continue
            if validate(rec):
                rej += 1
                rej_total += rec['amount']

        # Оригінали, перекваліфіковані у повернення після імпорту
        refunded = conn.execute("""
            SELECT COUNT(*) c, COALESCE(SUM(amount_original),0) s FROM donation
            WHERE source_id=? AND donation_status='refunded'
              AND refund_of_donation_id IS NULL
        """, (source_id,)).fetchone()

        adj_count = dup_count + rej + refunded['c']
        adj_total = dup_total + rej_total + refunded['s']

        residual = round(source_total - db_row['s'] - adj_total, 2)
        residual_count = source_count - db_row['c'] - adj_count

        status = 'matched' if abs(residual) < 0.01 and residual_count == 0 \
            else 'discrepancy'

        conn.execute(
            'INSERT INTO ops_reconciliation (recon_id,source_id,period_start,'
            'period_end,source_total,crm_total,difference,source_count,crm_count,'
            'status,checked_at,notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (new_id('rec'), source_id, '2023-01-01', '2026-09-02',
             round(source_total, 2), round(db_row['s'], 2), residual,
             source_count, db_row['c'], status, now(),
             f'дублікати={dup_count}, відхилено={rej}, повернення={refunded["c"]}'))

        results.append(dict(
            source=source_id, file=filename,
            source_count=source_count, db_count=db_row['c'],
            source_total=round(source_total, 2), db_total=round(db_row['s'], 2),
            duplicates=dup_count, rejected=rej, refunded=refunded['c'],
            adj_total=round(adj_total, 2), residual=residual,
            residual_count=residual_count, status=status))

    conn.commit()
    return results


def print_report(results: list[dict]):
    print('\nЗВІРКА З ДЖЕРЕЛАМИ')
    print(f'   {"канал":<14}{"джерело":>9}{"база":>7}{"сума джерела":>16}'
          f'{"сума в базі":>15}{"залишок":>11}{"статус":>14}')
    for r in results:
        mark = 'OK' if r['status'] == 'matched' else 'РОЗБІЖНІСТЬ'
        print(f'   {r["source"]:<14}{r["source_count"]:>9}{r["db_count"]:>7}'
              f'{r["source_total"]:>16,.2f}{r["db_total"]:>15,.2f}'
              f'{r["residual"]:>11,.2f}{mark:>14}')
        explain = []
        if r['duplicates']:
            explain.append(f'дублікатів {r["duplicates"]}')
        if r['rejected']:
            explain.append(f'відхилено {r["rejected"]}')
        if r['refunded']:
            explain.append(f'повернень {r["refunded"]}')
        if explain:
            print(f'      пояснено: {", ".join(explain)} '
                  f'(на суму {r["adj_total"]:,.2f})')
    print('\n   Залишок нуль означає, що кожна різниця між джерелом і базою')
    print('   має відому причину. Ненульовий залишок — привід зупинити')
    print('   автоматизацію і розібратись до наступного завантаження.')
