"""
Генератор тестових даних.

Мета — не "згенерувати 100 рядків", а відтворити ті конкретні
проблеми, через які ручна обробка донорських даних ламається.
Кожна пастка нижче позначена у TRAPS і має відповідати
конкретному механізму захисту в пайплайні.

Файли пишуться у форматі, характерному для кожного каналу:
різні назви колонок, різні формати сум і дат, різна повнота даних.
Це навмисно: уніфікований CSV зробив би завдання нереалістично простим.
"""

from __future__ import annotations

import csv
import json
import os
import random
import zlib
from datetime import datetime, timedelta

SEED = 20260902
random.seed(SEED)

BASE_DIR = os.path.join(os.path.dirname(__file__), '..', 'data')
INCOMING = os.path.join(BASE_DIR, 'incoming')


# ==========================================================================
# ПАСТКИ, закладені у дані
# ==========================================================================

TRAPS = [
    ('T01', 'Один донор у 3 каналах',
     'Ковальчук О. дає через сайт, PayPal і банк. Кирилиця vs латиниця, '
     'email тільки в одному каналі.'),
    ('T02', 'Gmail з крапками і +тегом',
     'o.lena.kovalchuk+kse@gmail.com та olenakovalchuk@gmail.com — одна скринька.'),
    ('T03', 'Повторний імпорт того самого файлу',
     'Виписка PayPal завантажена двічі. Має відсіктись за хешем файлу.'),
    ('T04', 'Ретрай вебхука',
     'Той самий external_id двічі всередині файлу сайту.'),
    ('T05', 'Два різні донори з дуже схожими іменами',
     'Мельник Іван та Мельничук Іван. Схожість 0.96 — НЕ можна зливати.'),
    ('T06', 'Тезки: однакове ім\'я, різні люди',
     'Двоє "Андрій Шевченко" з різними email і містами.'),
    ('T07', 'Пожертва без email і телефону',
     'Банківський переказ і чек. Матчинг тільки по імені -> ручний розгляд.'),
    ('T08', 'Три валюти',
     'UAH, USD, EUR. Без курсу на дату аналітика не сходиться.'),
    ('T09', 'Комісія платіжної системи',
     'PayPal: gross != net. Питання "скільки зібрали" має дві відповіді.'),
    ('T10', 'Повернення коштів',
     'Донат і чарджбек через 12 днів. Наївний SUM() покаже завищену суму.'),
    ('T11', 'Регулярна підписка з невдалим платежем',
     'Місячна підписка, один платіж failed -> має спрацювати dunning.'),
    ('T12', 'Різні формати сум',
     '"1 000,00" (банк) vs "1,000.00" (PayPal). Кома означає різне.'),
    ('T13', 'Нерозбірлива дата',
     'Рукописний чек з датою "14 березня" без року -> ручний розгляд.'),
    ('T14', 'Анонімна пожертва',
     'Готівка без даних донора. Має потрапити у фінанси, але не в CRM-профіль.'),
    ('T15', 'Донор-організація',
     'ТОВ замість фізособи. Логіка імені й подяки інша.'),
    ('T16', 'Зміна прізвища',
     'Та сама людина, той самий email, нове прізвище після шлюбу.'),
    ('T17', 'Некоректна сума',
     'Порожнє поле суми у виписці -> запис відхиляється, не обнуляється.'),
    ('T18', 'Донор без згоди на розсилку',
     'Є пожертва, немає opt-in -> у список комунікацій потрапити не може.'),
    ('T19', 'Великий донор',
     'Пожертва 250 000 UAH -> автолист вимикається, створюється задача.'),
    ('T20', 'Втрачений (lapsed) донор, який повернувся',
     'Давав у 2023, тиша 20 місяців, донат у 2026 -> reactivation, не welcome.'),
]


# ==========================================================================
# ДОВІДКОВІ ДАНІ
# ==========================================================================

CAMPAIGNS = ['winter_appeal_2024', 'emergency_2025', 'scholarship_fund',
             'annual_2025', 'rebuild_2026', None]

FUNDS = ['general', 'scholarship', 'humanitarian']

CITIES_UA = ['Київ', 'Львів', 'Одеса', 'Харків', 'Дніпро', 'Вінниця']
CITIES_INTL = [('Warsaw', 'PL'), ('Berlin', 'DE'), ('London', 'GB'),
               ('New York', 'US'), ('Toronto', 'CA'), ('Prague', 'CZ')]


# ==========================================================================
# "ІСТИНА": реальні люди, які стоять за записами
# ==========================================================================
# person_id тут — ground truth. Пайплайн його не бачить; він
# використовується тільки для оцінки якості матчингу наприкінці.

PEOPLE = [
    # --- T01, T02: один донор у трьох каналах, кирилиця vs латиниця ---
    dict(pid='P001', kind='individual',
         ua='Ковальчук Олена Петрівна', lat='Olena Kovalchuk',
         emails=['o.lena.kovalchuk+kse@gmail.com', 'olenakovalchuk@gmail.com'],
         phone='+38 (067) 123-45-67', city='Київ', country='UA', consent=True),

    # --- T05: майже тезка попереднього прізвища, але інша людина ---
    dict(pid='P002', kind='individual',
         ua='Мельник Іван Андрійович', lat='Ivan Melnyk',
         emails=['i.melnyk@ukr.net'], phone='0501112233',
         city='Львів', country='UA', consent=True),
    dict(pid='P003', kind='individual',
         ua='Мельничук Іван Богданович', lat='Ivan Melnychuk',
         emails=['melnychuk.ivan@gmail.com'], phone='0509998877',
         city='Одеса', country='UA', consent=True),

    # --- T06: справжні тезки ---
    dict(pid='P004', kind='individual',
         ua='Шевченко Андрій', lat='Andrii Shevchenko',
         emails=['a.shevchenko@i.ua'], phone='0631234567',
         city='Харків', country='UA', consent=True),
    dict(pid='P005', kind='individual',
         ua='Шевченко Андрій', lat='Andrii Shevchenko',
         emails=['shevchenko.andriy1988@gmail.com'], phone='0679876543',
         city='Дніпро', country='UA', consent=False),   # T18: без згоди

    # --- T16: зміна прізвища, email той самий ---
    dict(pid='P006', kind='individual',
         ua='Бондаренко Марія', lat='Mariia Bondarenko',
         alt_lat='Mariia Kravets',          # після шлюбу
         emails=['m.bondarenko@gmail.com'], phone='0671110022',
         city='Вінниця', country='UA', consent=True),

    # --- T19: великий донор ---
    dict(pid='P007', kind='individual',
         ua='Гриценко Володимир', lat='Volodymyr Hrytsenko',
         emails=['v.hrytsenko@outlook.com'], phone='0503334455',
         city='Київ', country='UA', consent=True, major=True),

    # --- T20: lapsed -> повернувся ---
    dict(pid='P008', kind='individual',
         ua='Ткаченко Софія', lat='Sofiia Tkachenko',
         emails=['sofia.tkachenko@ukr.net'], phone='0662223344',
         city='Львів', country='UA', consent=True, lapsed_return=True),

    # --- T11: регулярний донор з невдалим платежем ---
    dict(pid='P009', kind='individual',
         ua='Романюк Дмитро', lat='Dmytro Romaniuk',
         emails=['d.romaniuk@gmail.com'], phone='0674445566',
         city='Київ', country='UA', consent=True, recurring=True),

    # --- діаспора: латиниця з діакритикою, іноземні міста ---
    dict(pid='P010', kind='individual',
         ua='Зґурський Андрій', lat='Andrii Zghurskyi',
         emails=['a.zghurskyi@gmail.com'], phone='+48221234567',
         city='Warsaw', country='PL', consent=True),
    dict(pid='P011', kind='individual',
         ua=None, lat='Katarzyna Nowak',
         emails=['k.nowak@wp.pl'], phone='+48501234567',
         city='Warsaw', country='PL', consent=True),
    dict(pid='P012', kind='individual',
         ua=None, lat='Michael Brennan',
         emails=['m.brennan@protonmail.com'], phone='+12125551234',
         city='New York', country='US', consent=True),
    dict(pid='P013', kind='individual',
         ua=None, lat='Anna Schmidt',
         emails=['anna.schmidt@web.de'], phone='+493012345678',
         city='Berlin', country='DE', consent=True),
    dict(pid='P014', kind='individual',
         ua=None, lat='James Whitfield',
         emails=['j.whitfield@gmail.com'], phone='+442071234567',
         city='London', country='GB', consent=True, major=True),

    # --- звичайні донори для об'єму ---
    dict(pid='P015', kind='individual', ua='Лисенко Ольга', lat='Olha Lysenko',
         emails=['o.lysenko@ukr.net'], phone='0631119988', city='Київ',
         country='UA', consent=True),
    dict(pid='P016', kind='individual', ua='Павленко Юрій', lat='Yurii Pavlenko',
         emails=['y.pavlenko@gmail.com'], phone='0505558877', city='Одеса',
         country='UA', consent=True),
    dict(pid='P017', kind='individual', ua='Савченко Наталія', lat='Nataliia Savchenko',
         emails=['n.savchenko@i.ua'], phone='0671234000', city='Харків',
         country='UA', consent=True, recurring=True),
    dict(pid='P018', kind='individual', ua='Кравченко Петро', lat='Petro Kravchenko',
         emails=['p.kravchenko@ukr.net'], phone='0509871234', city='Дніпро',
         country='UA', consent=True),
    dict(pid='P019', kind='individual', ua='Мороз Вікторія', lat='Viktoriia Moroz',
         emails=['v.moroz@gmail.com'], phone='0632221100', city='Львів',
         country='UA', consent=False),
    dict(pid='P020', kind='individual', ua='Козак Богдан', lat='Bohdan Kozak',
         emails=['b.kozak@gmail.com'], phone='0664443322', city='Вінниця',
         country='UA', consent=True, recurring=True),
    dict(pid='P021', kind='individual', ua='Гуменюк Ірина', lat='Iryna Humeniuk',
         emails=['i.humeniuk@ukr.net'], phone='0678889900', city='Київ',
         country='UA', consent=True),
    dict(pid='P022', kind='individual', ua='Дяченко Сергій', lat='Serhii Diachenko',
         emails=['s.diachenko@i.ua'], phone='0502223311', city='Одеса',
         country='UA', consent=True),

    # --- T15: організації ---
    dict(pid='P023', kind='organization', ua='ТОВ "Технобуд"', lat='Tekhnobud LLC',
         emails=['finance@tehnobud.ua'], phone='0442223344', city='Київ',
         country='UA', consent=True, major=True),
    dict(pid='P024', kind='organization', ua=None, lat='Nordic Aid Foundation',
         emails=['grants@nordicaid.org'], phone='+46812345678', city='Stockholm',
         country='SE', consent=True, major=True),
]

PEOPLE_BY_ID = {p['pid']: p for p in PEOPLE}


# ==========================================================================
# ДОПОМІЖНІ
# ==========================================================================

def _stable(text: str) -> int:
    """
    Детермінований хеш.

    Вбудований hash() для рядків рандомізується між процесами
    (PYTHONHASHSEED), тому payer id та IBAN відрізнялися б при кожному
    запуску попри фіксований random.seed. Для тестових даних, які
    мають бути відтворюваними, це неприйнятно.
    """
    return zlib.crc32(text.encode())


def _rand_dt(start: datetime, end: datetime) -> datetime:
    delta = int((end - start).total_seconds())
    return start + timedelta(seconds=random.randint(0, delta))


def _fmt_ua_amount(x: float) -> str:
    """Український/європейський формат: пробіл на тисячі, кома на десяткові."""
    s = f'{x:,.2f}'.replace(',', '\u00a0').replace('.', ',')
    return s


def _fmt_us_amount(x: float) -> str:
    return f'{x:,.2f}'


def _pick_currency(person: dict) -> str:
    if person['country'] == 'UA':
        return 'UAH' if random.random() < 0.85 else 'USD'
    return random.choice(['USD', 'EUR', 'EUR'])


def _amount_for(person: dict, currency: str) -> float:
    if person.get('major'):
        base = random.choice([25000, 50000, 100000, 250000])
        return float(base if currency == 'UAH' else base / 40)
    if currency == 'UAH':
        return float(random.choice([200, 300, 500, 500, 1000, 1000, 2000, 5000]))
    return float(random.choice([10, 20, 25, 50, 50, 100, 200]))


# Реєстр згенерованих транзакцій -> ground truth
GROUND_TRUTH: list[dict] = []


def _register(external_id: str, source: str, pid: str | None, note: str = ''):
    GROUND_TRUTH.append(dict(external_id=external_id, source=source,
                             true_person_id=pid or '', note=note))


# ==========================================================================
# КАНАЛ 1: САЙТ (вебхук платіжного шлюзу, JSON-подібний CSV)
# ==========================================================================

def gen_website(rows_target: int = 45) -> list[dict]:
    """
    Найчистіше джерело: email обов'язковий, дати ISO, суми крапкою.
    Пастки: ретрай вебхука (T04), зміна прізвища (T16), відсутня згода (T18).
    """
    rows = []
    start, end = datetime(2023, 1, 15), datetime(2026, 8, 25)
    pool = [p for p in PEOPLE if p['emails']]

    for i in range(rows_target):
        p = random.choice(pool)
        dt = _rand_dt(start, end)
        cur = _pick_currency(p)
        amt = _amount_for(p, cur)
        ext = f'WEB-{100000 + i}'

        # T16: після 2025-06 Марія підписується новим прізвищем
        name = p['lat']
        if p['pid'] == 'P006' and dt > datetime(2025, 6, 1):
            name = p['alt_lat']

        # T02: Олена іноді вводить email з крапками й +тегом
        email = p['emails'][0]
        if p['pid'] == 'P001':
            email = random.choice(p['emails'])

        rows.append(dict(
            transaction_id=ext,
            created_at=dt.strftime('%Y-%m-%dT%H:%M:%SZ'),
            donor_name=name,
            donor_email=email,
            donor_phone=p['phone'] if random.random() < 0.6 else '',
            amount=f'{amt:.2f}',
            currency=cur,
            payment_method=random.choice(['card', 'card', 'apple_pay', 'google_pay']),
            campaign=random.choice(CAMPAIGNS) or '',
            fund=random.choice(FUNDS),
            is_recurring='true' if p.get('recurring') and random.random() < 0.5 else 'false',
            marketing_consent='true' if p.get('consent') else 'false',
            country=p['country'],
            city=p['city'],
        ))
        _register(ext, 'website', p['pid'])

    # --- T04: ретрай вебхука, той самий transaction_id ще раз ---
    dup = dict(rows[7])
    rows.insert(20, dup)
    _register(dup['transaction_id'], 'website', None, 'T04 webhook retry duplicate')

    # --- T19: велика пожертва ---
    big = PEOPLE_BY_ID['P007']
    ext = 'WEB-199999'
    rows.append(dict(
        transaction_id=ext,
        created_at='2026-07-14T09:12:00Z',
        donor_name=big['lat'], donor_email=big['emails'][0],
        donor_phone=big['phone'], amount='250000.00', currency='UAH',
        payment_method='card', campaign='rebuild_2026', fund='general',
        is_recurring='false', marketing_consent='true',
        country='UA', city='Київ',
    ))
    _register(ext, 'website', 'P007', 'T19 major gift')

    # --- T20: lapsed донор повертається ---
    soph = PEOPLE_BY_ID['P008']
    for ext, date_str, amt in [('WEB-150001', '2023-03-10T12:00:00Z', '500.00'),
                               ('WEB-150002', '2023-05-22T18:30:00Z', '300.00'),
                               ('WEB-190500', '2026-06-05T11:15:00Z', '1000.00')]:
        rows.append(dict(
            transaction_id=ext, created_at=date_str,
            donor_name=soph['lat'], donor_email=soph['emails'][0],
            donor_phone='', amount=amt, currency='UAH',
            payment_method='card', campaign='annual_2025', fund='general',
            is_recurring='false', marketing_consent='true',
            country='UA', city='Львів',
        ))
        _register(ext, 'website', 'P008', 'T20 lapsed -> reactivated')

    return rows


# ==========================================================================
# КАНАЛ 2: PAYPAL (експорт транзакцій, US-формат)
# ==========================================================================

def gen_paypal(rows_target: int = 35) -> list[dict]:
    """
    Пастки: комісія (T09), US-формат сум (T12), повернення (T10),
    невдалий платіж підписки (T11), латиниця замість кирилиці (T01).
    """
    rows = []
    start, end = datetime(2023, 2, 1), datetime(2026, 8, 20)
    pool = [p for p in PEOPLE if p['kind'] == 'individual']

    for i in range(rows_target):
        p = random.choice(pool)
        dt = _rand_dt(start, end)
        cur = _pick_currency(p)
        gross = _amount_for(p, cur)
        fee = round(gross * 0.034 + (10 if cur == 'UAH' else 0.30), 2)
        ext = f'PP-{5000000 + i}'

        rows.append(dict(
            **{'Transaction ID': ext,
               'Date': dt.strftime('%m/%d/%Y %H:%M'),
               'Name': p['lat'],
               'From Email Address': p['emails'][0] if random.random() < 0.8 else '',
               'Payer ID': f'PAYER{_stable(p["pid"]) % 10**8:08d}',
               'Gross': _fmt_us_amount(gross),
               'Fee': f'-{_fmt_us_amount(fee)}',
               'Net': _fmt_us_amount(gross - fee),
               'Currency': cur,
               'Status': 'Completed',
               'Type': 'Subscription Payment' if p.get('recurring') and random.random() < 0.4
                       else 'Donation Payment',
               'Subscription ID': f'I-{_stable(p["pid"]) % 10**10:010d}'
                                  if p.get('recurring') else '',
               'Reference Txn ID': '',
               'Country': p['country']}
        ))
        _register(ext, 'paypal', p['pid'])

    # --- T01: Олена через PayPal, БЕЗ email, тільки латинське ім'я ---
    ol = PEOPLE_BY_ID['P001']
    ext = 'PP-5900001'
    rows.append({'Transaction ID': ext, 'Date': '04/18/2026 14:03',
                 'Name': 'Olena Kovalchuk', 'From Email Address': '',
                 'Payer ID': 'PAYER77001122', 'Gross': '1,000.00', 'Fee': '-34.00',
                 'Net': '966.00', 'Currency': 'UAH', 'Status': 'Completed',
                 'Type': 'Donation Payment', 'Subscription ID': '',
                 'Reference Txn ID': '', 'Country': 'UA'})
    _register(ext, 'paypal', 'P001', 'T01 cross-channel, no email')

    # --- T10: донат і повернення ---
    m = PEOPLE_BY_ID['P012']
    rows.append({'Transaction ID': 'PP-5900010', 'Date': '05/03/2026 10:00',
                 'Name': m['lat'], 'From Email Address': m['emails'][0],
                 'Payer ID': 'PAYER31200045', 'Gross': '500.00', 'Fee': '-17.30',
                 'Net': '482.70', 'Currency': 'USD', 'Status': 'Completed',
                 'Type': 'Donation Payment', 'Subscription ID': '',
                 'Reference Txn ID': '', 'Country': 'US'})
    _register('PP-5900010', 'paypal', 'P012', 'T10 original gift')
    rows.append({'Transaction ID': 'PP-5900011', 'Date': '05/15/2026 08:41',
                 'Name': m['lat'], 'From Email Address': m['emails'][0],
                 'Payer ID': 'PAYER31200045', 'Gross': '-500.00', 'Fee': '17.30',
                 'Net': '-482.70', 'Currency': 'USD', 'Status': 'Refunded',
                 'Type': 'Refund', 'Subscription ID': '',
                 'Reference Txn ID': 'PP-5900010', 'Country': 'US'})
    _register('PP-5900011', 'paypal', 'P012', 'T10 refund of PP-5900010')

    # --- T11: підписка з невдалим платежем ---
    d = PEOPLE_BY_ID['P009']
    sub = 'I-DMYTRO0001'
    for n, (date_str, status) in enumerate([
            ('03/05/2026 09:00', 'Completed'), ('04/05/2026 09:00', 'Completed'),
            ('05/05/2026 09:00', 'Failed'),    ('06/05/2026 09:00', 'Completed')]):
        ext = f'PP-591000{n}'
        rows.append({'Transaction ID': ext, 'Date': date_str, 'Name': d['lat'],
                     'From Email Address': d['emails'][0], 'Payer ID': 'PAYER55009911',
                     'Gross': '300.00', 'Fee': '-20.20', 'Net': '279.80',
                     'Currency': 'UAH', 'Status': status,
                     'Type': 'Subscription Payment', 'Subscription ID': sub,
                     'Reference Txn ID': '', 'Country': 'UA'})
        _register(ext, 'paypal', 'P009', f'T11 subscription {status}')

    return rows


# ==========================================================================
# КАНАЛ 3: БАНКІВСЬКА ВИПИСКА (кирилиця, без email)
# ==========================================================================

def gen_bank(rows_target: int = 30) -> list[dict]:
    """
    Найважче джерело: немає email, ім'я кирилицею з по батькові,
    суми у форматі "1 000,00", призначення платежу довільним текстом.
    Пастки: T07 (немає контактів), T12 (формат сум), T17 (порожня сума).
    """
    rows = []
    start, end = datetime(2023, 3, 1), datetime(2026, 8, 15)
    pool = [p for p in PEOPLE if p['ua']]

    for i in range(rows_target):
        p = random.choice(pool)
        dt = _rand_dt(start, end)
        amt = _amount_for(p, 'UAH')
        ext = f'BNK-{2026000 + i}'
        iban = f'UA{_stable(p["pid"]) % 10**8:08d}0000026207{_stable(p["pid"]) % 10**6:06d}'

        rows.append({
            'Референс': ext,
            'Дата операції': dt.strftime('%d.%m.%Y'),
            'Платник': p['ua'],
            'IBAN платника': iban,
            'Сума': _fmt_ua_amount(amt),
            'Валюта': 'UAH',
            'Призначення платежу': random.choice([
                'Благодійна допомога', 'Добровільна пожертва',
                'Благодійний внесок згідно з публічною офертою',
                'Пожертва на статутну діяльність',
            ]),
        })
        _register(ext, 'bank_transfer', p['pid'])

    # --- T01: Олена банківським переказом, повне ім'я з по батькові ---
    rows.append({'Референс': 'BNK-2099001', 'Дата операції': '20.02.2026',
                 'Платник': 'Ковальчук Олена Петрівна',
                 'IBAN платника': 'UA773052990000026207777001122',
                 'Сума': '2\u00a0500,00', 'Валюта': 'UAH',
                 'Призначення платежу': 'Благодійна пожертва'})
    _register('BNK-2099001', 'bank_transfer', 'P001', 'T01 cyrillic + patronymic')

    # --- T05: Мельник і Мельничук в один день, обидва без контактів ---
    rows.append({'Референс': 'BNK-2099010', 'Дата операції': '11.03.2026',
                 'Платник': 'Мельник Іван Андрійович',
                 'IBAN платника': 'UA113052990000026207111000111',
                 'Сума': '1\u00a0000,00', 'Валюта': 'UAH',
                 'Призначення платежу': 'Пожертва'})
    _register('BNK-2099010', 'bank_transfer', 'P002', 'T05 similar name A')
    rows.append({'Референс': 'BNK-2099011', 'Дата операції': '11.03.2026',
                 'Платник': 'Мельничук Іван Богданович',
                 'IBAN платника': 'UA223052990000026207222000222',
                 'Сума': '1\u00a0000,00', 'Валюта': 'UAH',
                 'Призначення платежу': 'Пожертва'})
    _register('BNK-2099011', 'bank_transfer', 'P003', 'T05 similar name B — must NOT merge')

    # --- T15: пожертва від організації ---
    rows.append({'Референс': 'BNK-2099020', 'Дата операції': '05.05.2026',
                 'Платник': 'ТОВ "Технобуд"',
                 'IBAN платника': 'UA903052990000026007999000999',
                 'Сума': '150\u00a0000,00', 'Валюта': 'UAH',
                 'Призначення платежу': 'Благодійна допомога згідно з договором №14/26'})
    _register('BNK-2099020', 'bank_transfer', 'P023', 'T15 organization donor')

    # --- T17: порожня сума ---
    rows.append({'Референс': 'BNK-2099030', 'Дата операції': '17.06.2026',
                 'Платник': 'Лисенко Ольга',
                 'IBAN платника': 'UA553052990000026207555000555',
                 'Сума': '', 'Валюта': 'UAH',
                 'Призначення платежу': 'Пожертва'})
    _register('BNK-2099030', 'bank_transfer', None, 'T17 broken amount — must be rejected')

    return rows


# ==========================================================================
# КАНАЛ 4: ЧЕКИ / ГОТІВКА (ручне введення)
# ==========================================================================

def gen_checks() -> list[dict]:
    """
    Ручне введення: найбільше помилок, найменше структури.
    Пастки: T13 (нерозбірлива дата), T14 (анонім), T07 (немає контактів).
    """
    rows = [
        {'check_no': 'CHK-001', 'date_received': '12.01.2026',
         'donor': 'Кравченко Петро', 'address': 'м. Дніпро, вул. Січова 12',
         'amount': '5 000', 'currency': 'UAH', 'notes': 'чек, доставлено поштою'},
        {'check_no': 'CHK-002', 'date_received': '03.02.2026',
         'donor': 'Гуменюк Ірина', 'address': 'м. Київ',
         'amount': '2 000', 'currency': 'UAH', 'notes': ''},
        # T13: рік не вказано
        {'check_no': 'CHK-003', 'date_received': '14 березня',
         'donor': 'Савченко Наталія', 'address': 'м. Харків',
         'amount': '1 500', 'currency': 'UAH', 'notes': 'дата нерозбірлива'},
        # T14: анонім
        {'check_no': 'CHK-004', 'date_received': '22.04.2026',
         'donor': '', 'address': '', 'amount': '10 000', 'currency': 'UAH',
         'notes': 'анонімна пожертва, скринька в офісі'},
        {'check_no': 'CHK-005', 'date_received': '30.05.2026',
         'donor': 'Козак Богдан', 'address': 'м. Вінниця, вул. Соборна 3',
         'amount': '750', 'currency': 'UAH', 'notes': ''},
        # ще один запис Олени — тепер без email і в третьому написанні
        {'check_no': 'CHK-006', 'date_received': '18.06.2026',
         'donor': 'О. П. Ковальчук', 'address': 'м. Київ',
         'amount': '3 000', 'currency': 'UAH', 'notes': 'ініціали на чеку'},
        {'check_no': 'CHK-007', 'date_received': '02.07.2026',
         'donor': 'Nordic Aid Foundation', 'address': 'Stockholm, Sweden',
         'amount': '5 000', 'currency': 'EUR', 'notes': 'банківський чек, грант'},
    ]
    truth = ['P018', 'P021', 'P017', None, 'P020', 'P001', 'P024']
    notes = ['', '', 'T13 unparseable date', 'T14 anonymous', '',
             'T01/T07 initials only, no contacts', 'T15 org']
    for r, t, n in zip(rows, truth, notes):
        _register(r['check_no'], 'check', t, n)
    return rows


# ==========================================================================
# КУРСИ ВАЛЮТ
# ==========================================================================

def gen_fx() -> list[dict]:
    """Курси до UAH. Плавні коливання замість константи — щоб було видно,
    що перерахунок робиться на дату транзакції, а не поточним курсом."""
    rows = []
    d = datetime(2023, 1, 1)
    usd, eur = 36.6, 39.1
    while d <= datetime(2026, 9, 1):
        usd = round(min(45.0, max(36.0, usd + random.uniform(-0.06, 0.09))), 4)
        eur = round(min(50.0, max(38.0, eur + random.uniform(-0.07, 0.10))), 4)
        rows.append(dict(rate_date=d.strftime('%Y-%m-%d'), currency='USD', rate_to_base=usd))
        rows.append(dict(rate_date=d.strftime('%Y-%m-%d'), currency='EUR', rate_to_base=eur))
        rows.append(dict(rate_date=d.strftime('%Y-%m-%d'), currency='UAH', rate_to_base=1.0))
        d += timedelta(days=1)
    return rows


# ==========================================================================
# ЗАПИС ФАЙЛІВ
# ==========================================================================

def _write_csv(path: str, rows: list[dict]):
    if not rows:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def main():
    os.makedirs(INCOMING, exist_ok=True)

    website = gen_website()
    paypal = gen_paypal()
    bank = gen_bank()
    checks = gen_checks()
    fx = gen_fx()

    _write_csv(os.path.join(INCOMING, 'website_donations.csv'), website)
    _write_csv(os.path.join(INCOMING, 'paypal_transactions.csv'), paypal)
    _write_csv(os.path.join(INCOMING, 'bank_statement.csv'), bank)
    _write_csv(os.path.join(INCOMING, 'checks_manual.csv'), checks)
    _write_csv(os.path.join(BASE_DIR, 'fx_rates.csv'), fx)

    # T03: той самий файл PayPal, завантажений вдруге під іншою назвою.
    # Вміст ідентичний -> має відсіктись за хешем файлу, а не за рядками.
    _write_csv(os.path.join(INCOMING, 'paypal_transactions_REIMPORT.csv'), paypal)

    _write_csv(os.path.join(BASE_DIR, 'ground_truth.csv'), GROUND_TRUTH)

    with open(os.path.join(BASE_DIR, 'traps.json'), 'w', encoding='utf-8') as f:
        json.dump([dict(code=c, name=n, description=d) for c, n, d in TRAPS],
                  f, ensure_ascii=False, indent=2)

    print(f'Згенеровано (seed={SEED}):')
    print(f'  website_donations.csv            {len(website):>4} рядків')
    print(f'  paypal_transactions.csv          {len(paypal):>4} рядків')
    print(f'  paypal_transactions_REIMPORT.csv {len(paypal):>4} рядків  <- T03 дубль файлу')
    print(f'  bank_statement.csv               {len(bank):>4} рядків')
    print(f'  checks_manual.csv                {len(checks):>4} рядків')
    print(f'  fx_rates.csv                     {len(fx):>4} рядків')
    print(f'  ground_truth.csv                 {len(GROUND_TRUTH):>4} рядків')
    print(f'\nРеальних осіб/організацій: {len(PEOPLE)}')
    print(f'Закладено пасток: {len(TRAPS)}')


if __name__ == '__main__':
    main()
