#!/usr/bin/env python3
"""
Запуск повного циклу.

    python3 run.py

Кроки:
    1. генерація тестових даних
    2. завантаження й обробка (ingestion + матчинг)
    3. звірка з джерелами
    4. перерахунок метрик і сегментація
    5. автоматичні дії
    6. черга ручного розгляду та злиття донорів
    7. оцінка якості матчингу
    8. аналітичний звіт
    9. генерація HTML-дашборду
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

import actions
import analytics
import dashboard
import evaluate
import generate_data
import ingest
import merge
import metrics
import reconcile


def header(n: int, title: str):
    print()
    print('─' * 72)
    print(f' КРОК {n}. {title}')
    print('─' * 72)


def main():
    header(1, 'ГЕНЕРАЦІЯ ТЕСТОВИХ ДАНИХ')
    generate_data.main()

    header(2, 'ЗАВАНТАЖЕННЯ ТА ОБРОБКА')
    stats = ingest.run(fresh=True)
    conn = stats['conn']
    print(f'Завантажено пожертв      : {stats["loaded"]}')
    print(f'Анонімних                : {stats["anonymous"]}')
    print(f'Дублікатів відсічено     : {len(stats["duplicates"])}')
    for src, ext in stats['duplicates']:
        print(f'   {ext} ({src}) — повторне надходження тієї самої транзакції')
    print(f'Файлів пропущено         : {len(stats["files_skipped"])}')
    for f, orig in stats['files_skipped']:
        print(f'   {f} — ідентичний вміст уже завантажено з {orig}')
    print(f'Записів відхилено        : {len(stats["rejected"])}')
    for ext, errs in stats['rejected']:
        print(f'   {ext} — {", ".join(errs)}')
    print(f'Рішення матчера          : {stats["outcomes"]}')

    header(3, 'ЗВІРКА З ДЖЕРЕЛАМИ')
    reconcile.print_report(reconcile.reconcile(conn))

    header(4, 'МЕТРИКИ ТА СЕГМЕНТАЦІЯ')
    res = metrics.recalculate_all(conn)
    print(f'Оброблено донорів: {res["donors"]}, змін сегмента: {res["segment_changes"]}\n')
    print(f'   {"сегмент":<20}{"донорів":>9}{"сума, UAH":>14}'
          f'{"середня LTV":>14}{"пожертв":>10}')
    for r in metrics.segment_summary(conn):
        print(f'   {r["segment"]:<20}{r["donors"]:>9}{r["lifetime_value"]:>14,.0f}'
              f'{r["avg_lifetime"]:>14,.0f}{r["avg_gifts"]:>10}')

    header(5, 'АВТОМАТИЧНІ ДІЇ')
    act = actions.run_actions(conn)
    for k, v in sorted(act.items(), key=lambda x: -x[1]):
        print(f'   {k:<28} {v}')

    print('\n   СПИСОК ДЛЯ ПЕРСОНАЛЬНОГО FOLLOW-UP')
    print(f'   {"донор":<26}{"сегмент":<20}{"пож.":>5}{"LTV":>11}'
          f'{"тиша":>7}{"згода":>7}{"пріор.":>9}')
    for r in actions.followup_list(conn, 12):
        print(f'   {r["display_name"][:25]:<26}{r["segment"]:<20}{r["gifts"]:>5}'
              f'{r["lifetime"]:>11,.0f}{r["days_quiet"]:>7}{r["consent"]:>7}'
              f'{r["priority_score"]:>9}')

    header(6, 'ЧЕРГА РУЧНОГО РОЗГЛЯДУ')
    queue = merge.pending_reviews(conn, 6)
    print('Неоднозначні випадки — ті, де алгоритм не має права вирішувати сам.')
    print('Так це виглядає для оператора: профіль, кандидат, оцінка.\n')
    print(f'   {"новий профіль":<26}{"кандидат":<26}{"оцінка":>8}  підстава')
    for q in queue:
        print(f'   {q["new_name"][:25]:<26}{q["candidate_name"][:25]:<26}'
              f'{q["score"]:>8.3f}  {q["support"]}')
    # Демонстрація механізму, а не рішення по суті: система навмисно
    # не має права вирішувати за оператора, які саме профілі об'єднати.
    # Показуємо, що операція виконується і що вона оборотна.
    if queue:
        top = queue[0]
        before = conn.execute(
            'SELECT COUNT(*) c FROM donation WHERE donor_id=?',
            (top['candidate_id'],)).fetchone()['c']

        mid = merge.merge_donors(conn, top['candidate_id'], top['new_donor_id'],
                                 'demo', top['score'])
        after = conn.execute(
            'SELECT COUNT(*) c FROM donation WHERE donor_id=?',
            (top['candidate_id'],)).fetchone()['c']
        print(f'\n   Злиття {top["new_name"][:22]} -> {top["candidate_name"][:22]}')
        print(f'      пожертв на профілі-переможці: {before} -> {after}')
        print(f'      merge_id={mid}, знімок обох профілів збережено')

        merge.revert_merge(conn, mid)
        restored = conn.execute(
            'SELECT COUNT(*) c FROM donation WHERE donor_id=?',
            (top['candidate_id'],)).fetchone()['c']
        print(f'   Відкат: {after} -> {restored} пожертв, стан відновлено')
        print('\n   Рішення по суті ухвалює людина, яка бачить обидва профілі.')
        amb = len(merge.pending_reviews(conn, 500))
        total = conn.execute("SELECT COUNT(*) c FROM match_review_queue "
                             "WHERE status='open'").fetchone()['c']
        print(f'   Неоднозначних, що чекають рішення: {amb}')
        print(f'   Уся черга разом із підтвердженнями та битими записами: {total}')
        metrics.recalculate_all(conn)

    header(7, 'ЯКІСТЬ МАТЧИНГУ')
    conn.commit()
    evaluate.evaluate()

    header(8, 'АНАЛІТИКА')
    analytics.report(conn)

    header(9, 'REPORTING VIEW')
    dashboard.main()

    print('\nБаза: data/donors.db')


if __name__ == '__main__':
    main()
