"""
Нормалізація вхідних даних.

Це фундамент матчингу. Якщо нормалізація слабка, будь-який,
навіть найрозумніший алгоритм зіставлення працюватиме погано:
він просто не побачить, що "O.Lena.Kovalchuk+donate@GMAIL.com"
і "olena.kovalchuk@gmail.com" — та сама поштова скринька.

Свідомо без зовнішніх залежностей: прототип має запускатись
на чистому Python 3.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime

# --------------------------------------------------------------------------
# Транслітерація українська -> латиниця
# --------------------------------------------------------------------------
# Стандарт КМУ 55:2010. Потрібен, щоб зіставити "Ковальчук Олена"
# з банківської виписки та "Olena Kovalchuk" з PayPal — для української
# організації з міжнародними донорами це щоденний кейс, а не екзотика.

_TRANSLIT = {
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'h', 'ґ': 'g', 'д': 'd', 'е': 'e',
    'ж': 'zh', 'з': 'z', 'и': 'y', 'і': 'i', 'к': 'k', 'л': 'l', 'м': 'm',
    'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
    'ф': 'f', 'х': 'kh', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'shch',
    'ь': '', "'": '', 'ʼ': '', '’': '',
    # російські літери — донори з діаспори часто пишуть саме так
    'ы': 'y', 'э': 'e', 'ъ': '', 'ё': 'e',
}

# Літери, вимова яких залежить від позиції у слові
_TRANSLIT_INITIAL = {'є': 'ye', 'ї': 'yi', 'й': 'y', 'ю': 'yu', 'я': 'ya'}
_TRANSLIT_INNER = {'є': 'ie', 'ї': 'i', 'й': 'i', 'ю': 'iu', 'я': 'ia'}


def transliterate(text: str) -> str:
    """Кирилиця -> латиниця за КМУ 55:2010."""
    if not text:
        return ''
    out = []
    for word in text.split():
        chars = []
        for i, ch in enumerate(word):
            low = ch.lower()
            is_upper = ch.isupper()
            if low in _TRANSLIT_INITIAL:
                rep = _TRANSLIT_INITIAL[low] if i == 0 else _TRANSLIT_INNER[low]
            elif low == 'г' and i > 0 and word[i - 1].lower() == 'з':
                rep = 'gh'          # зг -> zgh, інакше плутається з ж
            elif low in _TRANSLIT:
                rep = _TRANSLIT[low]
            else:
                rep = low
            chars.append(rep.capitalize() if is_upper and rep else rep)
        out.append(''.join(chars))
    return ' '.join(out)


def _strip_accents(text: str) -> str:
    """Kovalčuk -> Kovalcuk. Європейські донори часто вводять з діакритикою."""
    nfkd = unicodedata.normalize('NFKD', text)
    return ''.join(c for c in nfkd if not unicodedata.combining(c))


# --------------------------------------------------------------------------
# Ім'я
# --------------------------------------------------------------------------

# Титули, суфікси та організаційно-правові форми, які не несуть
# інформації для зіставлення. Форма власності особливо важлива:
# без неї 'ТОВ "Технобуд"' (банківська виписка) і 'Tekhnobud LLC'
# (сайт) виглядають як дві різні організації.
_NAME_NOISE = {
    'mr', 'mrs', 'ms', 'dr', 'prof', 'sir', 'madam',
    'пан', 'пані', 'др', 'проф',
    'tov', 'pp', 'fop', 'llc', 'ltd', 'inc', 'gmbh', 'corp', 'plc',
    'ooo', 'zat', 'pat', 'bv', 'ab', 'oy', 'kft', 'sp', 'zoo',
}

# По батькові прибираємо: у банківській виписці воно є,
# у PayPal — ніколи. Порівнювати треба по спільному знаменнику.
# Перевірка відбувається ВЖЕ ПІСЛЯ транслітерації, тому закінчення
# мають бути в латинській формі ("Петрівна" -> "Petrivna" -> -ivna).
_PATRONYMIC_RE = re.compile(
    r'^\w{3,}(ovych|evych|yovych|iovych|ivna|yivna|ovna|evna)$', re.IGNORECASE)


def normalize_name(name: str) -> str:
    """
    Канонічна форма імені для зіставлення.

    Кроки: транслітерація -> нижній регістр -> зняття діакритики ->
    прибирання титулів, по батькові та пунктуації -> сортування токенів.

    Сортування токенів вирішує проблему порядку: "Ковальчук Олена"
    (банківський формат) і "Olena Kovalchuk" (PayPal) дають однаковий ключ.
    """
    if not name:
        return ''
    name = transliterate(name.strip())
    name = _strip_accents(name).lower()
    # Лапки прибираємо окремо: 'ТОВ "Технобуд"' інакше дає токен
    # '"tekhnobud"', який не збігається з 'tekhnobud' із іншого каналу.
    name = re.sub(r'["\'`\u00ab\u00bb\u201c\u201d.,\-_()\\/]', ' ', name)
    tokens = []
    for tok in name.split():
        if not tok or tok in _NAME_NOISE:
            continue
        if _PATRONYMIC_RE.match(tok):
            continue
        if len(tok) == 1:          # ініціал: для матчингу шуму більше, ніж користі
            continue
        tokens.append(tok)
    return ' '.join(sorted(tokens))


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------

_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[a-z]{2,}$', re.IGNORECASE)

# Провайдери, які ігнорують крапки в локальній частині адреси
_DOT_INSENSITIVE = {'gmail.com', 'googlemail.com'}


def normalize_email(email: str) -> str | None:
    """
    Нижній регістр, зняття +тегів, прибирання крапок для Gmail.

    Без цього кроку той самий донор, який один раз підписався
    olena.k@gmail.com, а другий — OlenaK+kse@gmail.com, стане
    двома записами в базі.
    """
    if not email:
        return None
    email = email.strip().lower()
    if not _EMAIL_RE.match(email):
        return None
    local, _, domain = email.partition('@')
    if domain == 'googlemail.com':
        domain = 'gmail.com'
    local = local.split('+', 1)[0]
    if domain in _DOT_INSENSITIVE:
        local = local.replace('.', '')
    return f'{local}@{domain}' if local else None


# --------------------------------------------------------------------------
# Телефон
# --------------------------------------------------------------------------

def normalize_phone(phone: str, default_country: str = '380') -> str | None:
    """До формату E.164. Українські номери дописуємо кодом країни."""
    if not phone:
        return None
    digits = re.sub(r'\D', '', phone)
    if not digits:
        return None
    if digits.startswith('00'):
        digits = digits[2:]
    if len(digits) == 9:                       # 671234567
        digits = default_country + digits
    elif len(digits) == 10 and digits.startswith('0'):   # 0671234567
        digits = default_country + digits[1:]
    if len(digits) < 10 or len(digits) > 15:
        return None
    return '+' + digits


# --------------------------------------------------------------------------
# Суми та дати
# --------------------------------------------------------------------------

_CURRENCY_SYMBOLS = {'$': 'USD', '€': 'EUR', '£': 'GBP', '₴': 'UAH'}


def parse_amount(value) -> tuple[float | None, str | None]:
    """
    Розбирає суму в довільному форматі, повертає (сума, валюта або None).

    Реальні формати з різних каналів:
      "1 000,00"    банківська виписка (український локаль)
      "1,000.00"    PayPal (US локаль)
      "$1,250.00"   з символом валюти
      "250.00 USD"  з кодом валюти
    Головна пастка — кома: у першому випадку це десятковий роздільник,
    у другому — розділювач тисяч. Плутанина тут дає похибку в 1000 разів.
    """
    if value is None:
        return None, None
    if isinstance(value, (int, float)):
        return float(value), None

    text = str(value).strip()
    if not text:
        return None, None

    currency = None
    for sym, code in _CURRENCY_SYMBOLS.items():
        if sym in text:
            currency = code
            text = text.replace(sym, '')
    m = re.search(r'\b([A-Z]{3})\b', text)
    if m:
        currency = m.group(1)
        text = text.replace(m.group(1), '')

    text = text.replace('\u00a0', '').replace(' ', '').strip()
    if not text:
        return None, currency

    if ',' in text and '.' in text:
        # Десятковим вважаємо той роздільник, що стоїть правіше
        text = (text.replace(',', '') if text.rfind('.') > text.rfind(',')
                else text.replace('.', '').replace(',', '.'))
    elif ',' in text:
        frac = text.rsplit(',', 1)[1]
        # "1,00" -> десяткова кома; "1,000" -> розділювач тисяч
        text = text.replace(',', '.') if len(frac) <= 2 else text.replace(',', '')

    try:
        return float(text), currency
    except ValueError:
        return None, currency


_DATE_FORMATS = [
    '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d',
    '%d.%m.%Y %H:%M', '%d.%m.%Y', '%d/%m/%Y',
    '%m/%d/%Y %H:%M', '%m/%d/%Y', '%d-%b-%Y', '%B %d, %Y',
]


def parse_date(value) -> datetime | None:
    """
    Дата з довільного каналу.

    Свідомо НЕ вгадуємо між 03/04/2025 та 04/03/2025 навмання:
    неоднозначні дати краще відправити на ручний розгляд, ніж
    тихо зсунути пожертву на місяць і зіпсувати звітність.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    text = re.sub(r'(Z|[+-]\d{2}:\d{2})$', '', text)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------
# Схожість рядків
# --------------------------------------------------------------------------

def jaro_winkler(s1: str, s2: str, prefix_weight: float = 0.1) -> float:
    """
    Jaro-Winkler similarity, 0..1.

    Обрано свідомо замість Левенштейна: він дає бонус за збіг початку
    рядка, а прізвища найчастіше різняться саме закінченням
    (Kovalchuk / Kovalchuck / Kovaltchouk). Для імен це працює
    помітно краще, ніж проста редакційна відстань.
    """
    if s1 == s2:
        return 1.0
    if not s1 or not s2:
        return 0.0

    len1, len2 = len(s1), len(s2)
    window = max(len1, len2) // 2 - 1
    if window < 0:
        window = 0

    s1_matches = [False] * len1
    s2_matches = [False] * len2
    matches = 0

    for i in range(len1):
        start, end = max(0, i - window), min(i + window + 1, len2)
        for j in range(start, end):
            if s2_matches[j] or s1[i] != s2[j]:
                continue
            s1_matches[i] = s2_matches[j] = True
            matches += 1
            break

    if matches == 0:
        return 0.0

    transpositions = 0
    k = 0
    for i in range(len1):
        if not s1_matches[i]:
            continue
        while not s2_matches[k]:
            k += 1
        if s1[i] != s2[k]:
            transpositions += 1
        k += 1
    transpositions //= 2

    jaro = (matches / len1 + matches / len2 +
            (matches - transpositions) / matches) / 3

    prefix = 0
    for a, b in zip(s1[:4], s2[:4]):
        if a != b:
            break
        prefix += 1

    return jaro + prefix * prefix_weight * (1 - jaro)


def name_similarity(name_a: str, name_b: str) -> float:
    """
    Схожість імен на нормалізованих формах.

    Порівнюємо і рядок цілком, і токени попарно: це рятує випадок,
    коли в одному джерелі є по батькові або друге ім'я, а в іншому ні.
    """
    a, b = normalize_name(name_a), normalize_name(name_b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0

    whole = jaro_winkler(a, b)

    tokens_a, tokens_b = a.split(), b.split()
    if not tokens_a or not tokens_b:
        return whole
    scores = []
    for ta in tokens_a:
        scores.append(max(jaro_winkler(ta, tb) for tb in tokens_b))
    token_score = sum(scores) / len(scores)

    return max(whole, token_score * 0.97)   # невеликий штраф за часткове покриття
