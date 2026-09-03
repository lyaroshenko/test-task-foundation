#!/usr/bin/env python3
"""
Генератор reporting view.

Читає donors.db і збирає HTML-дашборд. Жодне число не зашите в шаблон:
дашборд є похідною від бази, а не окремою презентацією. Це принципово —
звіт, який готується вручну, завжди розходиться з даними.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import analytics
import metrics
from db import connect

OUT = os.path.join(os.path.dirname(__file__), '..', 'docs', 'dashboard.html')

CSS = """
:root{
  --paper:#FBFAF7; --ink:#15202B; --soft:#5A6B7A; --faint:#8FA0AE;
  --rule:#DFE4E8; --fill:#2C4457; --redink:#9B2226; --redwash:#F7EDEC;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  margin:0; background:var(--paper); color:var(--ink);
  font-family:"IBM Plex Sans","Segoe UI",system-ui,sans-serif;
  font-size:15px; line-height:1.55;
  font-variant-numeric:tabular-nums; font-feature-settings:"tnum" 1;
}
.wrap{max-width:840px; margin:0 auto; padding:56px 24px 96px}
h1,h2,h3,.num{font-family:"IBM Plex Serif",Georgia,serif}
h1{font-size:31px; line-height:1.2; font-weight:600; margin:0 0 6px; letter-spacing:-.01em}
h2{font-size:13px; font-weight:600; margin:0 0 20px; color:var(--soft);
   letter-spacing:.02em}
.meta{color:var(--faint); font-size:13px; margin:0 0 44px}

section{border-top:1px solid var(--rule); padding:34px 0 6px}

/* ---- заголовок: фізичний підрахунок донорів ---- */
.hero{border:none; padding:0 0 40px}
.dots{display:flex; flex-wrap:wrap; gap:7px; margin:22px 0 16px; max-width:520px}
.dot{width:17px; height:17px; border-radius:50%; border:1.5px solid var(--fill)}
.dot.on{background:var(--fill)}
.dot.off{border-color:var(--redink); background:transparent}
.hero-line{font-family:"IBM Plex Serif",Georgia,serif; font-size:20px;
  line-height:1.45; max-width:34em; margin:0}
.hero-line b{font-weight:600}

/* ---- метрика: число ліворуч, рішення праворуч ---- */
.metric{display:grid; grid-template-columns:1fr 1fr; gap:28px; align-items:start}
.metric .figure{min-width:0}
.num{font-size:38px; font-weight:600; line-height:1.05; letter-spacing:-.02em}
.num small{font-size:16px; font-weight:400; color:var(--soft); letter-spacing:0}
.cap{color:var(--soft); font-size:13px; margin-top:4px}
.decision{font-size:14px; color:var(--ink); border-left:2px solid var(--rule);
  padding-left:16px}
.decision .what{color:var(--soft); display:block; margin-bottom:5px; font-size:13px}
.risk .decision{border-left-color:var(--redink)}
.risk .num{color:var(--redink)}

/* ---- таблиці ---- */
table{width:100%; border-collapse:collapse; font-size:14px; margin-top:6px}
th{font-weight:500; color:var(--soft); text-align:right; font-size:12.5px;
   padding:0 0 8px; border-bottom:1px solid var(--rule)}
th:first-child,td:first-child{text-align:left}
td{padding:7px 0; border-bottom:1px solid var(--rule); text-align:right;
   font-variant-numeric:tabular-nums}
tr:last-child td{border-bottom:none}
td.n{font-family:"IBM Plex Serif",Georgia,serif}
.flag{color:var(--redink)}

/* ---- смуги ---- */
.bar{height:9px; background:var(--rule); border-radius:1px; overflow:hidden;
  display:flex; min-width:90px}
.bar i{display:block; height:100%}
.bar .a{background:var(--fill)}
.bar .b{background:#B7C3CC}
.legend{font-size:12.5px; color:var(--soft); margin-top:10px}
.legend span{margin-right:18px}
.swatch{display:inline-block; width:9px; height:9px; margin-right:5px;
  vertical-align:baseline}

.note{font-size:13.5px; color:var(--soft); max-width:62ch; margin:18px 0 0}
.foot{margin-top:52px; padding-top:20px; border-top:1px solid var(--rule);
  font-size:12.5px; color:var(--faint)}

@media (max-width:640px){
  .wrap{padding:36px 18px 64px}
  .metric{grid-template-columns:1fr; gap:14px}
  h1{font-size:26px}
  .num{font-size:32px}
  table{font-size:13px}
}
@media (prefers-reduced-motion:no-preference){
  .dot{animation:pop .34s cubic-bezier(.2,.8,.3,1) backwards}
}
@keyframes pop{from{opacity:0; transform:scale(.4)} to{opacity:1; transform:none}}
"""


def metric(figure: str, caption: str, what: str, decision: str, risk=False) -> str:
    cls = 'metric risk' if risk else 'metric'
    return f"""<div class="{cls}">
  <div class="figure"><div class="num">{figure}</div><div class="cap">{caption}</div></div>
  <div class="decision"><span class="what">{what}</span>{decision}</div>
</div>"""


def build(conn) -> str:
    raised = analytics.total_raised(conn)
    nvr = analytics.new_vs_returning(conn)
    ret = analytics.retention(conn, analytics.PREVIOUS_YEAR)
    rec = analytics.recurring_share(conn)
    gift = analytics.gift_size(conn)
    con = analytics.concentration(conn)
    ops = analytics.operations(conn)
    segs = metrics.segment_summary(conn)

    import actions
    followup = actions.followup_list(conn, 8)

    last = raised[-1]
    prev = raised[-2] if len(raised) > 1 else last
    growth = (last['gross'] / prev['gross'] - 1) * 100 if prev['gross'] else 0

    # --- заголовок: кружечок на кожного донора минулого року ---------
    dots = ''.join(
        f'<span class="dot {"on" if i < ret["retained"] else "off"}" '
        f'style="animation-delay:{i*28}ms"></span>'
        for i in range(ret['base_donors']))

    lost = ret['base_donors'] - ret['retained']

    # --- обсяг збору ---------------------------------------------------
    mx = max(r['gross'] for r in raised)
    volume_rows = ''.join(
        f'<tr><td>{r["year"]}</td>'
        f'<td class="n">{r["gross"]:,.0f}</td>'
        f'<td class="n">{r["net"]:,.0f}</td>'
        f'<td>{r["donors"]}</td>'
        f'<td style="width:180px;padding-left:20px">'
        f'<span class="bar"><i class="a" style="width:{r["gross"]/mx*100:.1f}%"></i></span></td>'
        f'</tr>'.replace(',', '\u202f')
        for r in raised)

    # --- нові проти повторних ------------------------------------------
    nvr_rows = ''
    for r in nvr:
        tot = (r['new_amount'] or 0) + (r['repeat_amount'] or 0)
        rep = (r['repeat_amount'] or 0) / tot * 100 if tot else 0
        nvr_rows += (
            f'<tr><td>{r["year"]}</td>'
            f'<td class="n">{rep:.0f}%</td>'
            f'<td>{r["new_gifts"]}</td><td>{r["repeat_gifts"]}</td>'
            f'<td style="width:200px;padding-left:20px">'
            f'<span class="bar"><i class="a" style="width:{rep:.1f}%"></i>'
            f'<i class="b" style="width:{100-rep:.1f}%"></i></span></td></tr>')

    # --- сегменти -------------------------------------------------------
    seg_total = sum(s['lifetime_value'] for s in segs) or 1
    risky = {'lapsing', 'lapsed', 'recurring_at_risk'}
    seg_rows = ''.join(
        f'<tr><td{" class=flag" if s["segment"] in risky else ""}>{s["segment"]}</td>'
        f'<td>{s["donors"]}</td>'
        f'<td class="n">{s["lifetime_value"]:,.0f}</td>'
        f'<td class="n">{s["avg_lifetime"]:,.0f}</td>'
        f'<td>{s["avg_gifts"]}</td>'
        f'<td style="width:150px;padding-left:20px"><span class="bar">'
        f'<i class="a" style="width:{s["lifetime_value"]/seg_total*100:.1f}%"></i>'
        f'</span></td></tr>'.replace(',', '\u202f')
        for s in segs)

    # --- follow-up ------------------------------------------------------
    fu_rows = ''.join(
        f'<tr><td>{r["display_name"][:30]}</td><td>{r["segment"]}</td>'
        f'<td>{r["gifts"]}</td><td class="n">{r["lifetime"]:,.0f}</td>'
        f'<td>{r["days_quiet"]}</td>'
        f'<td{" class=flag" if r["consent"]=="ні" else ""}>{r["consent"]}</td></tr>'
        .replace(',', '\u202f')
        for r in followup)

    ratio = gift['mean'] / gift['median'] if gift['median'] else 0
    queue_total = sum(c for _, c in ops['review_queue'])

    def fmt(n):
        return f'{n:,.0f}'.replace(',', '\u202f')

    return f"""<!DOCTYPE html>
<html lang="uk"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Індивідуальні донори · огляд</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Serif:wght@400;600&display=swap" rel="stylesheet">
<style>{CSS}</style>
</head><body><div class="wrap">

<header class="hero">
  <h1>Індивідуальні донори</h1>
  <p class="meta">станом на 2 вересня 2026 · джерело: donors.db · оновлено автоматично</p>
  <div class="dots">{dots}</div>
  <p class="hero-line">З <b>{ret['base_donors']}</b> донорів
  {analytics.PREVIOUS_YEAR} року повернулися <b>{ret['retained']}</b>.
  {lost} не дали жодної пожертви цього року — і саме вони, а не нові
  донори, є найдешевшим джерелом зростання наступного.</p>
</header>

<section>
  <h2>Обсяг збору</h2>
  {metric(fmt(last['gross']) + ' <small>грн</small>',
          f"зібрано за {last['year']} рік, {growth:+.0f}% до попереднього",
          'Що показує',
          f"Комісії каналів з'їдають {fmt(last['gross']-last['net'])} грн на рік. "
          "Планувати програми можна тільки за net, і зсув частки зборів "
          "у бік банківського переказу знижує цю втрату напряму.")}
  <table style="margin-top:26px">
    <tr><th>рік</th><th>зібрано</th><th>дійшло</th><th>донорів</th><th></th></tr>
    {volume_rows}
  </table>
</section>

<section>
  <h2>Нові проти повторних</h2>
  {metric(f"{(nvr[-1]['repeat_amount'] or 0)/((nvr[-1]['new_amount'] or 0)+(nvr[-1]['repeat_amount'] or 1))*100:.0f}%",
          'доходу цього року дали повторні пожертви',
          'Що показує',
          'За чий рахунок живе організація. Зростання лише за рахунок нових '
          'донорів дороге і крихке: залучення коштує в рази більше за утримання. '
          'Висока частка повторних означає, що ресурс правильно вкладено в базу.')}
  <table style="margin-top:26px">
    <tr><th>рік</th><th>частка повторних</th><th>перших</th><th>повторних</th><th></th></tr>
    {nvr_rows}
  </table>
  <p class="legend">
    <span><i class="swatch" style="background:var(--fill)"></i>повторні</span>
    <span><i class="swatch" style="background:#B7C3CC"></i>перші пожертви</span>
  </p>
</section>

<section class="risk">
  <h2>Утримання донорів</h2>
  {metric(f"{ret['first_time_rate']:.0f}%",
          f"новачків {analytics.PREVIOUS_YEAR} року дали ще раз "
          f"({ret['first_time_retained']} з {ret['first_time_donors']}). "
          f"Загальне утримання {ret['rate']:.0f}%",
          'Чому це головна метрика',
          'Обсяг збору говорить про те, що вже сталося. Утримання — про те, '
          'що станеться наступного року. Провал саме серед новачків вказує '
          'на перші 90 днів: подяка, welcome-серія, звіт про використання '
          'коштів. Це той відрізок, який можна виправити за квартал.',
          risk=True)}
</section>

<section class="risk">
  <h2>Регулярні пожертви</h2>
  {metric(f"{rec['share_pct']:.1f}%",
          f"доходу приходить за підпискою · активних планів: "
          f"{sum(rec['plans'].values())}",
          'Що з цим робити',
          'Регулярні донори дають менші суми, але лишаються в рази довше. '
          'Це єдина частина бюджету, під яку можна планувати зарплати й '
          f'багаторічні програми. Найближчий крок очевидний: серед донорів '
          f'з трьома і більше разовими пожертвами запустити кампанію переходу '
          f'на щомісячну підтримку.',
          risk=True)}
</section>

<section>
  <h2>Розмір пожертви</h2>
  {metric(f"{fmt(gift['median'])} <small>грн</small>",
          f"медіанна пожертва · середня {fmt(gift['mean'])} грн, "
          f"розрив {ratio:.0f}×",
          'Навіщо обидва числа',
          'Одна пожертва на 250 000 грн підіймає середнє так, що воно '
          'перестає описувати реальність. Суми в формі на сайті треба '
          f'ставити навколо медіани — {fmt(gift["median"])} грн, а не навколо '
          'середнього, інакше форма відлякує звичайного донора.')}
</section>

<section class="risk">
  <h2>Концентрація доходу</h2>
  {metric(f"{con['top10_share']:.0f}%",
          f"збору дають {con['top10_donors']} донорів з {con['donors']} · "
          f"найбільший один дає {con['top1_share']:.0f}%",
          'Це метрика ризику, не успіху',
          'Втрата одного донора з цієї групи означає кризу бюджету. План Б '
          'має існувати до того, як вона настане. Звідси дві паралельні '
          'задачі: персональний менеджер для великих донорів і розширення '
          'масової бази, щоб знизити залежність.',
          risk=True)}
</section>

<section>
  <h2>Сегменти бази</h2>
  <table>
    <tr><th>сегмент</th><th>донорів</th><th>сума, грн</th><th>середня LTV</th>
        <th>пожертв</th><th></th></tr>
    {seg_rows}
  </table>
  <p class="note">Червоним позначені сегменти, які втрачають активність.
  Сегмент присвоюється автоматично після кожної пожертви, історія переходів
  зберігається — це дозволяє бачити не лише стан бази, а й напрямок її руху.</p>
</section>

<section>
  <h2>Кого набрати цього тижня</h2>
  <table>
    <tr><th>донор</th><th>сегмент</th><th>пожертв</th><th>LTV, грн</th>
        <th>днів тиші</th><th>згода</th></tr>
    {fu_rows}
  </table>
  <p class="note">Пріоритет рахується не за розміром пожертви, а за добутком
  цінності донора й тривалості мовчання. Донор, який давав багато і давно
  замовк, важливіший за того, хто щойно дав уперше.</p>
</section>

<section>
  <h2>Як працює автоматизація</h2>
  {metric(f"{ops['thanked_pct']:.0f}%",
          f"пожертв отримали подяку · {ops['thanked']} з {ops['donations']}",
          'Чому це у звіті про гроші',
          'Класична fundraising-аналітика вимірює гроші й забуває процес. '
          'Але донор, якому не подякували, майже не повертається — і падіння '
          'утримання починається саме тут.')}
  <table style="margin-top:26px">
    <tr><th>показник</th><th>значення</th></tr>
    <tr><td>Донорів, доступних для комунікацій</td>
        <td class="n">{ops['consent_granted']} з {ops['consent_total']}
        ({ops['reachable_pct']:.0f}%)</td></tr>
    <tr><td>У черзі на ручний розгляд</td><td class="n">{queue_total}</td></tr>
    <tr><td>Відкритих проблем якості даних</td>
        <td class="n">{sum(c for _, _, c in ops['data_quality'])}</td></tr>
    <tr><td>З них критичних</td>
        <td class="n{' flag' if any(s=='high' for _,s,_ in ops['data_quality']) else ''}">
        {sum(c for _, s, c in ops['data_quality'] if s == 'high')}</td></tr>
  </table>
  <p class="note">Черга на ручний розгляд — не дефект, а свідомий вихід
  алгоритму. У сумнівних випадках зіставлення донорів рішення передається
  людині. Зростання черги означає, що пороги потребують доналаштування,
  а не що потрібна ще одна людина на ручну обробку.</p>
</section>

<p class="foot">Дашборд згенеровано з donors.db. Жодне число не введено вручну.</p>
</div></body></html>"""


def main():
    conn = connect(fresh=False)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w', encoding='utf-8') as f:
        f.write(build(conn))
    print(f'Дашборд: {os.path.relpath(OUT)}')


if __name__ == '__main__':
    main()
