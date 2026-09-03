"""
Злиття донорів і закриття черги ручного розгляду.

Без цього модуля черга нікуди не веде: людина бачить кандидатів,
але не має чим зафіксувати рішення. Матчер свідомо передає сумнівні
випадки людині — значить, мусить існувати спосіб їх повернути.

Злиття оборотне за побудовою. Профіль-донор не видаляється, а
позначається; повний знімок обох записів до операції зберігається
в merge_log. Помилковий мердж у CRM реального фонду виявляється
через місяці, і на той момент відкат має бути можливим.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from db import new_id, now

# Таблиці, чиї записи переносяться на профіль-переможець
MOVE_TABLES = ('donation', 'donor_identifier', 'donor_consent',
               'communication', 'recurring_plan', 'donor_segment_history')


def _snapshot(conn, donor_id: str) -> dict:
    donor = conn.execute('SELECT * FROM donor WHERE donor_id=?', (donor_id,)).fetchone()
    return dict(
        donor=dict(donor) if donor else None,
        identifiers=[dict(r) for r in conn.execute(
            'SELECT * FROM donor_identifier WHERE donor_id=?', (donor_id,))],
        donation_ids=[r['donation_id'] for r in conn.execute(
            'SELECT donation_id FROM donation WHERE donor_id=?', (donor_id,))],
    )


def merge_donors(conn, winner_id: str, loser_id: str,
                 actor: str = 'operator', score: float | None = None) -> str:
    """
    Переносить усе з loser на winner і позначає loser як злитий.

    Переможцем має бути старіший профіль: він частіше вже фігурує
    у зовнішніх звітах і листуванні, і зберегти саме його дешевше.
    """
    if winner_id == loser_id:
        raise ValueError('Переможець і донор для злиття збігаються')

    snapshot = {'winner': _snapshot(conn, winner_id),
                'loser': _snapshot(conn, loser_id)}

    for table in MOVE_TABLES:
        conn.execute(f'UPDATE {table} SET donor_id=? WHERE donor_id=?',
                     (winner_id, loser_id))

    # Метрики переможця стають недійсними — видаляємо, наступний
    # перерахунок побудує їх заново вже з повною історією.
    conn.execute('DELETE FROM donor_metrics WHERE donor_id IN (?,?)',
                 (winner_id, loser_id))

    conn.execute(
        "UPDATE donor SET merged_into_donor_id=?, donor_status='merged', "
        'updated_at=? WHERE donor_id=?', (winner_id, now(), loser_id))

    merge_id = new_id('mrg')
    conn.execute(
        'INSERT INTO merge_log (merge_id,winner_donor_id,loser_donor_id,'
        'merged_at,merged_by,match_score,snapshot_before,is_reverted) '
        'VALUES (?,?,?,?,?,?,?,0)',
        (merge_id, winner_id, loser_id, now(), actor, score,
         json.dumps(snapshot, ensure_ascii=False, default=str)))
    conn.commit()
    return merge_id


def revert_merge(conn, merge_id: str) -> bool:
    """
    Відкат злиття зі знімка.

    Повертає ідентифікатори та пожертви на початковий профіль.
    Комунікації, створені вже після злиття, лишаються на переможці:
    вони справді були надіслані, і переписувати цей факт не можна.
    """
    row = conn.execute('SELECT * FROM merge_log WHERE merge_id=? AND is_reverted=0',
                       (merge_id,)).fetchone()
    if not row:
        return False
    snapshot = json.loads(row['snapshot_before'])
    loser_id = row['loser_donor_id']

    for did in snapshot['loser']['donation_ids']:
        conn.execute('UPDATE donation SET donor_id=? WHERE donation_id=?',
                     (loser_id, did))
    for idf in snapshot['loser']['identifiers']:
        conn.execute('UPDATE donor_identifier SET donor_id=? WHERE identifier_id=?',
                     (loser_id, idf['identifier_id']))

    conn.execute("UPDATE donor SET merged_into_donor_id=NULL, "
                 "donor_status='active', updated_at=? WHERE donor_id=?",
                 (now(), loser_id))
    conn.execute('DELETE FROM donor_metrics WHERE donor_id IN (?,?)',
                 (row['winner_donor_id'], loser_id))
    conn.execute('UPDATE merge_log SET is_reverted=1 WHERE merge_id=?', (merge_id,))
    conn.commit()
    return True


def pending_reviews(conn, limit: int = 20) -> list[dict]:
    """
    Черга з розкритими кандидатами — те, що побачив би оператор.
    """
    out = []
    rows = conn.execute(
        "SELECT review_id, raw_id, candidates, top_score, reason FROM "
        "match_review_queue WHERE status='open' AND reason='ambiguous_match' "
        'ORDER BY top_score DESC LIMIT ?', (limit,)).fetchall()
    for r in rows:
        cands = json.loads(r['candidates'] or '[]')
        if not cands:
            continue
        current = conn.execute(
            'SELECT dn.donor_id, d.display_name FROM donation dn '
            'JOIN donor d ON d.donor_id=dn.donor_id WHERE dn.raw_id=?',
            (r['raw_id'],)).fetchone()
        if not current:
            continue
        top = cands[0]
        top_name = conn.execute('SELECT display_name FROM donor WHERE donor_id=?',
                                (top['donor_id'],)).fetchone()
        out.append(dict(
            review_id=r['review_id'], reason=r['reason'],
            new_donor_id=current['donor_id'], new_name=current['display_name'],
            candidate_id=top['donor_id'],
            candidate_name=top_name['display_name'] if top_name else '?',
            score=top['score'], support=top.get('support', ''),
            n_candidates=len(cands)))
    return out


def resolve(conn, review_id: str, same_person: bool,
            actor: str = 'operator') -> str | None:
    """
    Фіксує рішення оператора.

    same_person=True  -> злити профілі
    same_person=False -> лишити роздільно, позначити чергу як опрацьовану
    """
    item = next((i for i in pending_reviews(conn, 500)
                 if i['review_id'] == review_id), None)
    if not item:
        return None

    merge_id = None
    if same_person:
        merge_id = merge_donors(conn, item['candidate_id'], item['new_donor_id'],
                                actor, item['score'])

    conn.execute(
        "UPDATE match_review_queue SET status='resolved', resolved_at=?, "
        'resolved_by=?, resolution=? WHERE review_id=?',
        (now(), actor, 'merged' if same_person else 'kept_separate', review_id))
    conn.commit()
    return merge_id
