"""
Оцінка якості матчингу.

Порівнюємо кластери, які побудував пайплайн, з ground truth.
Метрика — попарна: для кожної пари транзакцій питаємо
"алгоритм вважає їх одним донором?" і "чи це справді один донор?".

Ключова ідея: precision і recall тут не рівноцінні.
Хибне злиття (втрата precision) — дорога помилка.
Розщеплений донор (втрата recall) — дешева й видима в черзі.
Тому цільовий профіль: precision близька до 1.0 навіть ціною recall.
"""

from __future__ import annotations

import csv
import os
import sys
from collections import defaultdict
from itertools import combinations

sys.path.insert(0, os.path.dirname(__file__))
from db import connect

DATA = os.path.join(os.path.dirname(__file__), '..', 'data')


def load_truth() -> dict[str, str]:
    with open(os.path.join(DATA, 'ground_truth.csv'), encoding='utf-8') as f:
        return {r['external_id']: r['true_person_id']
                for r in csv.DictReader(f) if r['true_person_id']}


def evaluate():
    conn = connect(fresh=False)
    truth = load_truth()

    rows = conn.execute(
        'SELECT external_transaction_id ext, donor_id, match_method, match_confidence '
        'FROM donation WHERE external_transaction_id IS NOT NULL').fetchall()

    pairs = [(r['ext'], r['donor_id'], r['match_method']) for r in rows
             if r['ext'] in truth]

    # --------------------------------------------------------------- попарно
    tp = fp = fn = tn = 0
    false_merges = []
    for (e1, d1, _), (e2, d2, _) in combinations(pairs, 2):
        same_pred = d1 == d2
        same_true = truth[e1] == truth[e2]
        if same_pred and same_true:
            tp += 1
        elif same_pred and not same_true:
            fp += 1
            false_merges.append((e1, e2, truth[e1], truth[e2], d1))
        elif not same_pred and same_true:
            fn += 1
        else:
            tn += 1

    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0

    # ------------------------------------------------------------- кластери
    pred_to_true = defaultdict(set)
    true_to_pred = defaultdict(set)
    for ext, donor_id, _ in pairs:
        pred_to_true[donor_id].add(truth[ext])
        true_to_pred[truth[ext]].add(donor_id)

    contaminated = {d: t for d, t in pred_to_true.items() if len(t) > 1}
    fragmented = {t: d for t, d in true_to_pred.items() if len(d) > 1}

    # --------------------------------------------------------------- вивід
    print('=' * 66)
    print('ЯКІСТЬ МАТЧИНГУ'.center(66))
    print('=' * 66)
    print(f'Транзакцій з відомою істиною : {len(pairs)}')
    print(f'Реальних осіб                : {len(true_to_pred)}')
    print(f'Профілів створено            : {len(pred_to_true)}')
    print()
    print(f'  Precision : {precision:.4f}   (чи не злили різних людей)')
    print(f'  Recall    : {recall:.4f}   (чи знайшли всі збіги)')
    print(f'  F1        : {f1:.4f}')
    print(f'  TP={tp}  FP={fp}  FN={fn}  TN={tn}')
    print()

    print(f'ХИБНІ ЗЛИТТЯ (критична помилка): {len(contaminated)}')
    if contaminated:
        for d, t in contaminated.items():
            print(f'   ! профіль {d} містить осіб {sorted(t)}')
    else:
        print('   немає — жоден профіль не змішує різних людей')
    print()

    print(f'РОЗЩЕПЛЕНІ ДОНОРИ (дешева помилка, видима в черзі): {len(fragmented)}')
    for t, d in sorted(fragmented.items()):
        print(f'   {t}: {len(d)} профілі')
    print()

    # ------------------------------------------------- розподіл за методами
    print('РІШЕННЯ ЗА МЕТОДАМИ')
    by_method = conn.execute(
        'SELECT match_method, COUNT(*) c, ROUND(AVG(match_confidence),3) conf '
        'FROM donation GROUP BY match_method ORDER BY c DESC').fetchall()
    for r in by_method:
        print(f'   {r["match_method"] or "—":22} {r["c"]:>4}   середня впевненість {r["conf"]}')
    print()

    # --------------------------------------------------- перевірка пасток
    print('ПЕРЕВІРКА КЛЮЧОВИХ ПАСТОК')
    checks = []

    # T05: Мельник vs Мельничук не мають опинитись в одному профілі
    mel = conn.execute(
        "SELECT external_transaction_id ext, donor_id FROM donation "
        "WHERE external_transaction_id IN ('BNK-2099010','BNK-2099011')").fetchall()
    if len(mel) == 2:
        checks.append(('T05 Мельник / Мельничук не злиті',
                       mel[0]['donor_id'] != mel[1]['donor_id']))

    # T06: два Шевченки — різні профілі
    shev = true_to_pred.get('P004', set()) & true_to_pred.get('P005', set())
    checks.append(('T06 тезки Шевченки не злиті', not shev))

    # T10: повернення позначене
    ref = conn.execute(
        "SELECT donation_status FROM donation WHERE external_transaction_id='PP-5900010'"
    ).fetchone()
    checks.append(('T10 оригінал позначено як refunded',
                   ref and ref['donation_status'] == 'refunded'))

    # T11: підписка відновилась після невдалого платежу
    plan = conn.execute(
        "SELECT status FROM recurring_plan WHERE external_subscription_id='I-DMYTRO0001'"
    ).fetchone()
    checks.append(('T11 підписка активна після failed -> completed',
                   plan and plan['status'] == 'active'))

    # T14: анонім не має профілю з іменем
    anon = conn.execute(
        "SELECT COUNT(*) c FROM donation WHERE is_anonymous=1 "
        "AND donor_id='donor_anonymous'").fetchone()['c']
    checks.append(('T14 анонімна пожертва в системному профілі', anon == 1))

    # T15: організації розпізнані
    orgs = conn.execute(
        "SELECT COUNT(*) c FROM donor WHERE donor_type='organization'").fetchone()['c']
    checks.append((f'T15 організації розпізнані ({orgs})', orgs >= 2))

    # T18: донор без згоди існує і не має granted
    noc = conn.execute(
        "SELECT COUNT(*) c FROM donor_consent WHERE consent_type='email_marketing' "
        "AND status IN ('revoked','never_asked')").fetchone()['c']
    checks.append((f'T18 донори без згоди на розсилку ({noc})', noc > 0))

    # T08: усі три валюти перераховані
    curs = conn.execute(
        'SELECT COUNT(DISTINCT currency) c FROM donation').fetchone()['c']
    rates = conn.execute(
        'SELECT COUNT(*) c FROM donation WHERE currency<>"UAH" AND fx_rate=1.0'
    ).fetchone()['c']
    checks.append((f'T08 валют у базі: {curs}, без курсу: {rates}',
                   curs >= 3 and rates == 0))

    for label, ok in checks:
        print(f'   {"OK " if ok else "FAIL"}  {label}')

    print()
    print('=' * 66)
    return dict(precision=precision, recall=recall, f1=f1,
                contaminated=len(contaminated), fragmented=len(fragmented))


if __name__ == '__main__':
    evaluate()
