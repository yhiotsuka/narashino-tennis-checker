"""習志野市 公営テニスコート 空き状況チェッカー（ちば施設予約システム PC版）

ログイン不要の「施設名から探す → 空き状況」画面を閲覧するだけ。予約・ログイン・カート操作は一切しない。
使い方は README.md を参照。
"""
import argparse
import csv
import datetime as dt
import html
import json
import os
import sys
import time
import webbrowser
from pathlib import Path

import jpholiday
from playwright.sync_api import sync_playwright

BASE = "https://www.cm1.eprs.jp/yoyaku-chiba/w/"
VENUES = {
    "U4020": "秋津総合運動公園",
    "U4030": "芝園公園",
    "U4040": "袖ケ浦運動公園",
    "U4070": "実籾テニスコート",
}
INTERVAL = 3.0  # 遷移ごとの最低待ち秒数
MAX_DAYS_DEFAULT = 120  # 受付期間外が出ない場合の安全上限
ROOT = Path(__file__).resolve().parent
WEEKDAYS = "月火水木金土日"


class PageError(Exception):
    pass


class Site:
    """1つのブラウザセッションで画面遷移を管理する。遷移の間隔は必ず INTERVAL 秒以上空ける。"""

    def __init__(self, browser):
        self.browser = browser
        self.ctx = None
        self.pg = None
        self.last_nav = 0.0
        self.nav_count = 0

    def _new_session(self):
        if self.ctx:
            self.ctx.close()
        self.ctx = self.browser.new_context(locale="ja-JP")
        self.pg = self.ctx.new_page()
        self.pg.on("dialog", lambda d: d.dismiss())  # 確認ダイアログは常に拒否

    def _nav(self, action):
        """action() で遷移を起こし、読み込み完了とエラー画面を確認する"""
        wait = INTERVAL - (time.monotonic() - self.last_nav)
        if wait > 0:
            time.sleep(wait)
        with self.pg.expect_navigation(wait_until="load", timeout=60000):
            action()
        self.last_nav = time.monotonic()
        self.nav_count += 1
        body = self.pg.inner_text("body")
        if "エラー" in self.pg.title() or "システムエラーが発生しました" in body:
            raise PageError(f"エラー画面: {self.pg.title()} / {body.strip()[:80]}")

    def open_venue(self, bcd):
        """トップ → 施設名検索 → 検索結果 → 館の空き状況画面 まで辿る"""
        self._new_session()
        self._nav(lambda: self.pg.goto(BASE, wait_until="commit"))
        self._nav(lambda: self.pg.evaluate("doAction(document.form1, gRsvUTransNameSearchAction)"))
        self._nav(lambda: self.pg.evaluate("""() => { const f = document.childForm;
            f.querySelector('[name=checkCommunityCd][value=U4]').checked = true;
            f.textKeyword.value = 'テニス';
            doNameSearch(f, gRsvUNameSearchAction); }"""))
        self._nav(lambda: self.pg.evaluate(f"doSelect(document.childForm, gRsvUInstSrchVacantAction, '{bcd}')"))

    def fetch_date(self, date):
        """表示中の館で、テニスコートのみ・指定日の全ページを取得してコマのリストを返す"""
        self._nav(lambda: self.pg.evaluate(f"""() => {{
            document.getElementsByName('chkIcd').forEach(e =>
                e.checked = /テニス/.test(e.parentElement.innerText));
            $('#year').val({date.year}); $('#month').val({date.month}); $('#day').val({date.day});
            doReloadCheck(document.form1, gRsvUInstSrchVacantAction); }}"""))
        slots = self._parse()
        page = 1
        while self.pg.evaluate("!!document.getElementById('goNextPager')"):
            page += 1
            self._nav(lambda: self.pg.evaluate(f"movePage({page})"))
            slots += self._parse()
        ymd = date.strftime("%Y%m%d")
        if any(s["date"] != ymd for s in slots):
            raise PageError(f"要求日 {ymd} と表示日が一致しない")
        return slots

    def _parse(self):
        return self.pg.evaluate("""() => [...document.querySelectorAll('td.time-table2')].map(td => {
            const img = td.querySelector('img');
            const si = td.querySelector('input[name=selectInfo]');
            if (!si) return null;
            const v = si.value.split(':');
            const court = td.closest('table.tablebg2').querySelector('font.font-navy').innerText.replace('●', '').trim();
            return {court, icd: v[1], date: v[2], start: v[4], end: v[5], status: img ? img.alt : td.innerText.trim()};
        }).filter(Boolean)""")


def target_dates(start, max_days, all_days):
    for i in range(max_days):
        d = start + dt.timedelta(days=i)
        if all_days or d.weekday() >= 5 or jpholiday.is_holiday(d):
            yield d


def day_label(d):
    hol = jpholiday.is_holiday_name(d)
    return f"{d.month}/{d.day}({WEEKDAYS[d.weekday()]})" + (f" {hol}" if hol else "")


def run(args):
    start = dt.datetime.strptime(args.start, "%Y%m%d").date() if args.start else dt.date.today()
    max_days = args.days or MAX_DAYS_DEFAULT
    dates = list(target_dates(start, max_days, args.all_days))
    if args.count:
        dates = dates[:args.count]
    venues = args.venues or list(VENUES)
    records, notes = [], []
    t0 = time.monotonic()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        site = Site(browser)
        for bcd in venues:
            vname = VENUES[bcd]
            print(f"[{vname}] 開始", flush=True)
            try:
                with_retry(lambda: site.open_venue(bcd), f"{vname} 画面遷移")
            except Exception as e:
                notes.append(f"{vname}: 空き状況画面に到達できず中止 ({e})")
                print("  ! " + notes[-1], flush=True)
                continue
            for d in dates:
                def once():
                    try:
                        return site.fetch_date(d)
                    except Exception:
                        site.open_venue(bcd)  # セッションを作り直してから再試行させる
                        raise
                try:
                    slots = with_retry(once, f"{vname} {d}")
                except Exception as e:
                    notes.append(f"{vname} {d}: 取得失敗のため以降をスキップ ({e})")
                    print("  ! " + notes[-1], flush=True)
                    break
                if not slots or all(s["status"] == "受付期間外" for s in slots):
                    notes.append(f"{vname}: {d} は受付期間外 → 打ち切り（最終取得日はその前の対象日）")
                    print("  " + notes[-1], flush=True)
                    break
                free = sum(s["status"] == "空き" for s in slots)
                print(f"  {day_label(d)}: {len(slots)}コマ 空き{free}", flush=True)
                for s in slots:
                    records.append({"venue_cd": bcd, "venue": vname, **s})
        browser.close()
    elapsed = time.monotonic() - t0
    return records, notes, elapsed, site.nav_count


def with_retry(fn, label):
    try:
        return fn()
    except Exception as e:
        print(f"  ! {label}: {e} → 1回だけ再試行", flush=True)
        return fn()


def unbooked_month_notes(records):
    """館×月で「予約」が1コマも無いのに「空き」がある月は、抽選・予約受付前で空き表示になっている可能性が高い"""
    stat = {}
    for r in records:
        k = (r["venue"], r["date"][:6])
        st = stat.setdefault(k, {"空き": 0, "予約": 0})
        if r["status"] in st:
            st[r["status"]] += 1
    return [f"{v}: {m[:4]}年{int(m[4:])}月は予約済コマが0件（抽選・予約受付前の可能性。空き表示でも先着予約できるとは限らない）"
            for (v, m), st in sorted(stat.items()) if st["予約"] == 0 and st["空き"] > 0]


def previous_states(path):
    """前回の latest.json から コマ → 状態 を読む（無ければ空）"""
    try:
        with open(path, encoding="utf-8") as f:
            return {slot_key(r): r["status"] for r in json.load(f)["slots"]}
    except Exception:
        return {}


def slot_key(r):
    return (r["venue_cd"], r["icd"], r["date"], r["start"])


def write_outputs(records, notes, elapsed, navs, generated):
    notes = notes + unbooked_month_notes(records)
    out = ROOT / "results"
    out.mkdir(exist_ok=True)
    # 前回は空きでなかったのに今回空いたコマに NEW 印（前回に無い日付は対象外）
    prev = previous_states(out / "latest.json")
    for r in records:
        r["new"] = r["status"] == "空き" and prev.get(slot_key(r), "空き") != "空き"
    stem = generated.strftime("%Y%m%d_%H%M")
    meta = {"generated": generated.isoformat(timespec="seconds"), "elapsed_sec": round(elapsed),
            "page_loads": navs, "free_count": sum(r["status"] == "空き" for r in records), "notes": notes}
    with open(out / f"{stem}.json", "w", encoding="utf-8") as f:
        json.dump({**meta, "slots": records}, f, ensure_ascii=False, indent=1)
    with open(out / f"{stem}.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "venue", "court", "start", "end", "status", "new", "venue_cd", "icd"])
        w.writeheader()
        w.writerows(records)
    with open(out / "latest.json", "w", encoding="utf-8") as f:
        json.dump({**meta, "slots": records}, f, ensure_ascii=False, indent=1)
    html_path = out / "latest.html"
    html_path.write_text(render_html(records, meta), encoding="utf-8")
    return out / f"{stem}.json", html_path


# 予約サイトの入口（途中画面のURLはセッション切れになるため、トップに飛ばす）
BOOKING_SP = "https://www.cm1.eprs.jp/yoyaku-chiba/sp/"
BOOKING_PC = "https://www.cm1.eprs.jp/yoyaku-chiba/w/"


def time_band(start):
    h = int(start[:2])
    return "am" if h < 12 else "pm" if h < 17 else "night"


def hhmm(s):
    return f"{int(s[:2])}:{s[2:]}"


def short_venue(name):
    return name.replace("総合運動公園", "").replace("運動公園", "").replace("公園", "").replace("テニスコート", "")


def short_court(name):
    return name.replace("テニスコート", "")


def render_html(records, meta):
    # 日付 → 館 → コート → コマ
    tree = {}
    for r in records:
        tree.setdefault(r["date"], {}).setdefault(r["venue"], {}).setdefault(r["court"], []).append(r)
    parts = []
    for ymd in sorted(tree):
        d = dt.datetime.strptime(ymd, "%Y%m%d").date()
        day_free = sum(r["status"] == "空き" for v in tree[ymd].values() for c in v.values() for r in c)
        # 日付ごとの詳細は折りたたみ（タップで開く）
        parts.append(f'<details class="day{" has-free" if day_free else ""}">'
                     f'<summary><h2>{html.escape(day_label(d))} <span class="cnt">空き {day_free}</span></h2></summary>')
        for venue, courts in tree[ymd].items():
            v_free = sum(r["status"] == "空き" for c in courts.values() for r in c)
            parts.append(f'<div class="venue{" has-free" if v_free else ""}"><h3>{html.escape(venue)}</h3><table>')
            for court, slots in courts.items():
                c_free = any(s["status"] == "空き" for s in slots)
                cells = "".join(
                    f'<td class="{"free" if s["status"] == "空き" else "busy"}" title="{hhmm(s["start"])}–{hhmm(s["end"])} {html.escape(s["status"])}">'
                    f'<span class="t">{hhmm(s["start"])}</span>'
                    f'<span class="s">{"○空き" if s["status"] == "空き" else html.escape(s["status"])}</span></td>'
                    for s in slots)
                parts.append(f'<tr class="{"has-free" if c_free else ""}"><th>{html.escape(short_court(court))}</th>{cells}</tr>')
            parts.append("</table></div>")
        parts.append("</details>")
    # 先頭に置く空き一覧（スマホで最初に見たい情報）: 日付カード＋時間チップ
    cards = []
    for ymd in sorted(tree):
        rows = []
        for v, courts in tree[ymd].items():
            by_time = {}
            for c, slots in courts.items():
                for r in slots:
                    if r["status"] == "空き":
                        t = by_time.setdefault(r["start"], {"courts": [], "new": False})
                        t["courts"].append(short_court(c))
                        t["new"] |= r.get("new", False)
            if by_time:
                chips = "".join(
                    f'<span class="chip{" new" if t["new"] else ""}" data-band="{time_band(st)}">'
                    f'<b>{hhmm(st)}</b><small>{html.escape("・".join(t["courts"]))}</small></span>'
                    for st, t in sorted(by_time.items()))
                rows.append(f'<div class="vrow"><div class="vn">{html.escape(short_venue(v))}</div><div class="chips">{chips}</div></div>')
        if rows:
            d = dt.datetime.strptime(ymd, "%Y%m%d").date()
            n = sum(r["status"] == "空き" for c in tree[ymd].values() for sl in c.values() for r in sl)
            cards.append(f'<div class="card"><div class="ch">{html.escape(day_label(d))}<span class="cn">空き {n}</span></div>{"".join(rows)}</div>')
    new_count = sum(r.get("new", False) for r in records)
    filters = "".join(f'<button type="button" data-f="{k}"{" class=on" if k == "all" else ""}>{label}</button>'
                      for k, label in [("all", "すべて"), ("am", "午前"), ("pm", "午後"), ("night", "夜 17時〜")])
    summary = (f'<section class="free"><div class="fh"><h2>空き一覧 <span class="cn">{meta["free_count"]}コマ'
               f'{f"（NEW {new_count}）" if new_count else ""}</span></h2><div class="filters">{filters}</div></div>'
               f'{"".join(cards)}<p class="none" hidden>この時間帯の空きはありません。</p></section>'
               if cards else '<p class="summary">空きはありません。</p>')
    notes = "".join(f"<li>{html.escape(n)}</li>" for n in meta["notes"])
    gen = dt.datetime.fromisoformat(meta["generated"]).strftime("%Y-%m-%d %H:%M")
    body = "".join(parts) or "<p>データがありません。</p>"
    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>テニスコート空き状況</title>
<style>
:root {{ --bg:#fff; --fg:#1f2328; --muted:#6b7280; --line:#e5e7eb; --free:#16a34a; --free-bg:#dcfce7; --busy-bg:#f3f4f6; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#111418; --fg:#e6e6e6; --muted:#9aa3ad; --line:#2a2f36; --free:#4ade80; --free-bg:#14532d; --busy-bg:#1c2127; }} }}
body {{ margin:0; padding:16px; background:var(--bg); color:var(--fg); font-family:system-ui,"Hiragino Sans","Yu Gothic UI",sans-serif; }}
header {{ position:sticky; top:0; z-index:10; background:var(--bg); padding-bottom:8px; border-bottom:1px solid var(--line); }}
h1 {{ font-size:20px; margin:0 0 4px; }} .meta {{ color:var(--muted); font-size:13px; }}
label {{ display:inline-block; margin-top:8px; font-size:14px; cursor:pointer; }}
h2 {{ font-size:17px; margin:0; display:inline; }}
details.day {{ border-bottom:1px solid var(--line); padding:10px 0; }}
details.day > summary {{ cursor:pointer; list-style:none; }} details.day > summary::-webkit-details-marker {{ display:none; }}
details.day > summary::before {{ content:"▸"; display:inline-block; width:1.1em; color:var(--muted); transition:transform .15s; }}
details.day[open] > summary::before {{ transform:rotate(90deg); }}
details.day:not(.has-free) .cnt {{ color:var(--muted); }} h3 {{ font-size:14px; margin:12px 0 4px; color:var(--muted); }}
.cnt {{ font-size:13px; font-weight:normal; color:var(--free); }}
.venue {{ overflow-x:auto; }} table {{ border-collapse:collapse; }}
th {{ text-align:left; padding:4px 8px; white-space:nowrap; font-weight:600; font-size:13px; }}
td {{ padding:4px 6px; border:2px solid var(--bg); background:var(--busy-bg); color:var(--muted); font-size:12px; min-width:84px; text-align:center; }}
td .t {{ display:block; }} td .s {{ display:block; font-size:11px; }}
td.free {{ background:var(--free-bg); color:var(--free); font-weight:700; }}
body.only-free details.day:not(.has-free), body.only-free .venue:not(.has-free), body.only-free tr:not(.has-free), body.only-free td.busy {{ display:none; }}
ul.notes {{ color:var(--muted); font-size:12px; }}
.summary {{ margin:12px 0; font-size:14px; }}
.free {{ margin:12px 0 20px; }} .fh h2 {{ display:block; margin:0 0 8px; }}
.filters {{ display:flex; gap:6px; flex-wrap:wrap; margin-bottom:10px; }}
.filters button {{ border:1px solid var(--line); background:var(--bg); color:var(--fg); border-radius:999px; padding:6px 12px; font-size:13px; cursor:pointer; }}
.filters button.on {{ background:var(--free); border-color:var(--free); color:var(--bg); font-weight:700; }}
.card {{ border:1px solid var(--line); border-radius:12px; padding:10px 12px; margin:10px 0; box-shadow:0 1px 2px rgba(0,0,0,.04); }}
.ch {{ font-size:16px; font-weight:700; margin-bottom:6px; display:flex; justify-content:space-between; align-items:baseline; }}
.cn {{ font-size:13px; font-weight:600; color:var(--free); }}
.vrow {{ display:flex; gap:8px; align-items:flex-start; padding:6px 0; border-top:1px dashed var(--line); }}
.vrow:first-of-type {{ border-top:0; }}
.vn {{ flex:0 0 3.6em; font-weight:700; font-size:14px; padding-top:6px; }}
.chips {{ display:flex; flex-wrap:wrap; gap:6px; }}
.chip {{ position:relative; display:inline-flex; flex-direction:column; align-items:center; min-width:56px; padding:5px 8px;
  border-radius:10px; background:var(--free-bg); color:var(--free); line-height:1.2; }}
.chip b {{ font-size:16px; }} .chip small {{ font-size:11px; opacity:.9; }}
.chip.new::after {{ content:"NEW"; position:absolute; top:-7px; right:-6px; background:#ef4444; color:#fff; font-size:9px; font-weight:700; padding:1px 4px; border-radius:6px; }}
.hide {{ display:none !important; }}
.links {{ display:flex; gap:12px; align-items:center; margin-top:8px; font-size:13px; }}
.links a {{ color:var(--muted); }}
.links a.book {{ background:var(--free); color:var(--bg); text-decoration:none; font-weight:700; padding:7px 14px; border-radius:999px; }}
@media (max-width:600px) {{
  body {{ padding:12px; }} h1 {{ font-size:17px; }}
  th {{ padding:3px 4px; font-size:12px; }}
  td {{ min-width:0; width:46px; padding:3px 1px; font-size:11px; }} td .s {{ font-size:10px; }}
}}
</style></head>
<body>
<header>
<h1>習志野市 テニスコート空き状況</h1>
<div class="meta">生成日時 {gen}　／　空き {meta["free_count"]} コマ　／　所要 {meta["elapsed_sec"]} 秒・{meta["page_loads"]} 遷移</div>
<div class="links"><a class="book" href="{BOOKING_SP}" target="_blank" rel="noopener">予約サイトを開く（スマホ版）</a><a href="{BOOKING_PC}" target="_blank" rel="noopener">PC版</a></div>
<label><input type="checkbox" id="onlyFree"> 空きのみ表示</label>
</header>
{summary}
<div class="meta">日付をタップすると、コートごとの詳細が開きます。</div>
{body}
<ul class="notes">{notes}</ul>
<p class="meta">出典: ちば施設予約システム（閲覧のみ）。予約は<a href="{BOOKING_SP}" target="_blank" rel="noopener">公式サイト</a>から行ってください。</p>
<script>
const cb = document.getElementById('onlyFree');
try {{ cb.checked = localStorage.getItem('onlyFree') === '1'; }} catch (e) {{}}
const apply = () => {{ document.body.classList.toggle('only-free', cb.checked); try {{ localStorage.setItem('onlyFree', cb.checked ? '1' : '0'); }} catch (e) {{}} }};
cb.addEventListener('change', apply); apply();
// 時間帯フィルタ: 該当しないチップを隠し、空になった館・日付も隠す
const btns = document.querySelectorAll('.filters button');
const setBand = (f) => {{
  btns.forEach(b => b.classList.toggle('on', b.dataset.f === f));
  document.querySelectorAll('.chip').forEach(c => c.classList.toggle('hide', f !== 'all' && c.dataset.band !== f));
  document.querySelectorAll('.vrow').forEach(r => r.classList.toggle('hide', !r.querySelector('.chip:not(.hide)')));
  let any = false;
  document.querySelectorAll('.card').forEach(c => {{ const on = !!c.querySelector('.vrow:not(.hide)'); c.classList.toggle('hide', !on); any ||= on; }});
  const none = document.querySelector('.free .none'); if (none) none.hidden = any;
  try {{ localStorage.setItem('band', f); }} catch (e) {{}}
}};
btns.forEach(b => b.addEventListener('click', () => setBand(b.dataset.f)));
let saved = 'all'; try {{ saved = localStorage.getItem('band') || 'all'; }} catch (e) {{}}
setBand(saved);
</script>
</body></html>
"""


def main():
    ap = argparse.ArgumentParser(description="習志野市テニスコート空き状況チェッカー（閲覧のみ）")
    ap.add_argument("--all-days", action="store_true", help="平日も含める（既定は土日祝のみ）")
    ap.add_argument("--days", type=int, help=f"今日から何日先までを対象にするか（既定: 受付期間の終わりまで、最大{MAX_DAYS_DEFAULT}日）")
    ap.add_argument("--count", type=int, help="対象日を先頭から何日分にするか（例: 7 で土日祝7日分）")
    ap.add_argument("--start", help="開始日 YYYYMMDD（既定: 今日）")
    ap.add_argument("--venues", nargs="+", choices=list(VENUES), help="対象館コード（既定: 4館すべて）")
    ap.add_argument("--open", action="store_true", help="終了後に latest.html をブラウザで開く")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # コンソールの文字コードに無い文字で落ちないように
    generated = dt.datetime.now()
    records, notes, elapsed, navs = run(args)
    json_path, html_path = write_outputs(records, notes, elapsed, navs, generated)
    free = sum(r["status"] == "空き" for r in records)
    print(f"完了: {len(records)}コマ中 空き{free}  所要{elapsed:.0f}秒 / {navs}遷移")
    print(f"出力: {json_path} ほか / {html_path}")
    if args.open:
        webbrowser.open(html_path.as_uri())
    if not records:
        sys.exit("1件も取得できませんでした（サイトに到達できない可能性）")


if __name__ == "__main__":
    main()
