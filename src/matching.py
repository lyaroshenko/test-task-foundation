"""
Матчинг донорів (identity resolution).

Центральна частина рішення. Задача: за неповним і брудним записом
з довільного каналу вирішити, чи це вже відомий нам донор.

Головний принцип — асиметрія ціни помилки:

  Хибне злиття (два різні донори стали одним) коштує дорого і
  виправляється вручну тижнями: історія пожертв перемішана,
  людина отримує чужу подяку, звітність спотворена.

  Пропущене злиття (той самий донор двічі в базі) коштує дешево:
  видно в черзі на розгляд, зливається одним рухом.

Тому алгоритм свідомо консервативний: у сумнівних випадках він
не вгадує, а віддає рішення людині.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from normalize import (name_similarity, normalize_email, normalize_name,
                       normalize_phone)

# --------------------------------------------------------------------------
# Пороги
# --------------------------------------------------------------------------

# Мінімальна схожість імені, щоб кандидат узагалі розглядався
NAME_CANDIDATE_MIN = 0.90

# Схожість, вище якої запис можна прив'язати автоматично (з позначкою)
NAME_PROVISIONAL_MIN = 0.95

# Мінімальний відрив від другого кандидата. Якщо два донори схожі
# майже однаково — алгоритм не має права обирати між ними.
# Саме це правило рятує пару Мельник / Мельничук.
AMBIGUITY_MARGIN = 0.08


@dataclass
class MatchResult:
    donor_id: str | None = None
    method: str = 'new_donor'
    confidence: float = 0.0
    # auto        — прив'язано автоматично, ручний розгляд не потрібен
    # provisional — прив'язано, але позначено на перевірку
    # ambiguous   — НЕ прив'язано: створено новий профіль + запис у чергу
    # new         — донора не знайдено, створено новий профіль
    outcome: str = 'new'
    reason: str = ''
    candidates: list[dict] = field(default_factory=list)


class DonorMatcher:
    """
    Тримає індекси в пам'яті й оновлює їх на льоту.

    Оновлення на льоту принципове: якщо в одному файлі та сама нова
    людина зустрічається двічі, другий запис має знайти донора,
    створеного першим, а не породити ще один дубль.
    """

    def __init__(self, conn):
        self.conn = conn
        # (id_type, normalized_value) -> donor_id
        self.identifier_index: dict[tuple[str, str], str] = {}
        # донор -> {тип ідентифікатора: множина значень}
        # Потрібно для перевірки конфліктів: якщо у кандидата вже є email,
        # і він інший — це доказ, що перед нами інша людина.
        self.donor_identifiers: dict[str, dict[str, set]] = {}
        # літера -> [(donor_id, normalized_name, city, country)]
        self.name_blocks: dict[str, list[tuple]] = {}
        self._load()

    # ---------------------------------------------------------------- індекси

    @staticmethod
    def blocking_keys(normalized_name: str) -> set[str]:
        """
        Ключі блокування: перша літера КОЖНОГО токена імені, окремо.

        Донор індексується під усіма своїми ключами, пошук іде по об'єднанню.
        Це принципово для неповних записів: чек, підписаний "О. П. Ковальчук",
        дає єдиний токен 'kovalchuk' і ключ 'k'. Якби ключ будувався зі
        всіх літер одразу ('ko'), такий запис ніколи не знайшов би
        існуючий профіль "Kovalchuk Olena" і породив би дубль.
        """
        toks = normalized_name.split()
        return {t[0] for t in toks if t} or {'_'}

    def _load(self):
        for r in self.conn.execute(
                'SELECT id_type, id_value_normalized, donor_id FROM donor_identifier'):
            self.identifier_index[(r['id_type'], r['id_value_normalized'])] = r['donor_id']
            self.donor_identifiers.setdefault(r['donor_id'], {}).setdefault(
                r['id_type'], set()).add(r['id_value_normalized'])
        for r in self.conn.execute(
                'SELECT donor_id, name_normalized, city, country FROM donor '
                'WHERE merged_into_donor_id IS NULL'):
            if not r['name_normalized']:
                continue
            self.register_donor(r['donor_id'], r['name_normalized'],
                                r['city'], r['country'])

    def register_donor(self, donor_id: str, normalized_name: str,
                       city: str | None, country: str | None):
        if not normalized_name:
            return
        entry = (donor_id, normalized_name, city, country)
        for key in self.blocking_keys(normalized_name):
            self.name_blocks.setdefault(key, []).append(entry)

    def register_identifier(self, id_type: str, value_normalized: str, donor_id: str):
        self.identifier_index[(id_type, value_normalized)] = donor_id
        self.donor_identifiers.setdefault(donor_id, {}).setdefault(
            id_type, set()).add(value_normalized)

    def has_conflict(self, donor_id: str, id_type: str, value: str) -> bool:
        """
        Чи суперечить значення тому, що вже відоме про донора.

        Конфлікт = у донора вже є ідентифікатор цього типу, і він інший.
        Відсутність ідентифікатора конфліктом НЕ є: банківський переказ
        просто не містить email, і це нічого не спростовує.
        """
        known = self.donor_identifiers.get(donor_id, {}).get(id_type)
        return bool(known) and value not in known

    # ---------------------------------------------------------------- матчинг

    def match(self, rec: dict) -> MatchResult:
        """
        rec — нормалізований запис пожертви. Очікувані ключі:
        name, email, phone, processor_id, bank_account_hash, city, country.

        Рівні перевіряються за спаданням надійності. Перший спрацьований
        виграє: немає сенсу робити нечітке порівняння імен, якщо email збігся.
        """

        # --- Рівень 1: ідентифікатор платіжної системи -------------------
        # Найнадійніший сигнал: PayPal payer id або хеш банківського
        # рахунку прив'язані до конкретної особи самим провайдером.
        for id_type, key in (('paypal_payer_id', 'processor_id'),
                             ('bank_account_hash', 'bank_account_hash')):
            val = rec.get(key)
            if val:
                donor_id = self.identifier_index.get((id_type, val))
                if donor_id:
                    return MatchResult(donor_id, 'exact_processor_id', 1.0, 'auto',
                                       f'Збіг {id_type}')

        # --- Рівень 2: email --------------------------------------------
        email = normalize_email(rec.get('email') or '')
        if email:
            donor_id = self.identifier_index.get(('email', email))
            if donor_id:
                return MatchResult(donor_id, 'exact_email', 0.98, 'auto',
                                   'Збіг нормалізованого email')

        # --- Рівень 3: телефон ------------------------------------------
        # Слабший за email: номери переходять до інших власників,
        # у сім'ї буває спільний номер. Тому впевненість нижча.
        phone = normalize_phone(rec.get('phone') or '')
        if phone:
            donor_id = self.identifier_index.get(('phone', phone))
            if donor_id:
                return MatchResult(donor_id, 'exact_phone', 0.92, 'auto',
                                   'Збіг нормалізованого телефону')

        # --- Рівень 4: нечітке ім'я + підтверджувальний сигнал -----------
        name_norm = normalize_name(rec.get('name') or '')
        if not name_norm:
            return MatchResult(None, 'new_donor', 0.0, 'new', 'Немає даних для зіставлення')

        candidates = self._name_candidates(name_norm, rec, email, phone)
        if not candidates:
            return MatchResult(None, 'new_donor', 0.0, 'new', 'Кандидатів не знайдено')

        # Кандидати з конфліктом ідентифікаторів відкидаємо повністю.
        # Двоє людей з однаковим іменем і різними email — це двоє людей.
        clean = [c for c in candidates if not c['conflict']]
        if not clean:
            return MatchResult(
                None, 'new_donor', 0.0, 'new',
                'Ім\'я збігається, але ідентифікатори суперечать — інша особа',
                candidates)

        best = clean[0]
        second = clean[1] if len(clean) > 1 else None
        margin = best['score'] - second['score'] if second else 1.0

        # Кілька кандидатів з близькими оцінками: алгоритм не обирає.
        if margin < AMBIGUITY_MARGIN:
            return MatchResult(
                None, 'fuzzy_name', best['score'], 'ambiguous',
                f'Неоднозначно: {len(clean)} кандидати, відрив {margin:.3f}',
                clean)

        if best['score'] >= NAME_PROVISIONAL_MIN:
            # Прив'язуємо, але позначаємо. Команда бачить коректну історію
            # донора одразу, людина підтверджує рішення пізніше.
            return MatchResult(
                best['donor_id'], 'fuzzy_name', best['score'], 'provisional',
                f'Збіг за іменем {best["score"]:.3f}, {best["support"]}', clean)

        return MatchResult(None, 'fuzzy_name', best['score'], 'ambiguous',
                           f'Схожість {best["score"]:.3f} нижча за поріг', clean)

    def _name_candidates(self, name_norm: str, rec: dict,
                         email: str | None, phone: str | None) -> list[dict]:
        """Порівнюємо в об'єднанні блоків. Місто, країна та ідентифікатори — сигнали."""
        city, country = rec.get('city'), rec.get('country')
        seen, out = set(), []

        pool = []
        for key in self.blocking_keys(name_norm):
            pool.extend(self.name_blocks.get(key, []))

        for donor_id, cand_name, cand_city, cand_country in pool:
            if donor_id in seen:
                continue
            seen.add(donor_id)

            score = name_similarity(name_norm, cand_name)
            if score < NAME_CANDIDATE_MIN:
                continue

            support, conflict = [], False

            # Негативний доказ: сильний ідентифікатор, який суперечить.
            if email and self.has_conflict(donor_id, 'email', email):
                conflict = True
                support.append('КОНФЛІКТ email')
            if phone and self.has_conflict(donor_id, 'phone', phone):
                conflict = True
                support.append('КОНФЛІКТ телефону')

            if city and cand_city and city == cand_city:
                score = min(1.0, score + 0.02)
                support.append('місто збігається')
            elif city and cand_city and city != cand_city:
                # Різні міста послаблюють гіпотезу, але не спростовують:
                # люди переїжджають, а адреса в CRM застаріває.
                score -= 0.03
                support.append('різні міста')
            if country and cand_country and country != cand_country:
                score -= 0.05
                support.append('різні країни')

            # Запис із самим лише прізвищем проти повного імені: збіг
            # можливий, але слабший — не даємо йому дійти до впевненості 1.0.
            if min(len(name_norm.split()), len(cand_name.split())) == 1 \
                    and max(len(name_norm.split()), len(cand_name.split())) > 1:
                score = min(score, 0.96)
                support.append('лише частина імені')

            out.append(dict(donor_id=donor_id, score=round(score, 4),
                            matched_name=cand_name, conflict=conflict,
                            support=', '.join(support) or 'без підтвердження'))
        return sorted(out, key=lambda c: -c['score'])


def candidates_json(candidates: list[dict]) -> str:
    return json.dumps(candidates, ensure_ascii=False)
