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


def write_outputs(records, notes, elapsed, navs, generated):
    notes = notes + unbooked_month_notes(records)
    out = ROOT / "results"
    out.mkdir(exist_ok=True)
    stem = generated.strftime("%Y%m%d_%H%M")
    meta = {"generated": generated.isoformat(timespec="seconds"), "elapsed_sec": round(elapsed),
            "page_loads": navs, "free_count": sum(r["status"] == "空き" for r in records), "notes": notes}
    with open(out / f"{stem}.json", "w", encoding="utf-8") as f:
        json.dump({**meta, "slots": records}, f, ensure_ascii=False, indent=1)
    with open(out / f"{stem}.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "venue", "court", "start", "end", "status", "venue_cd", "icd"])
        w.writeheader()
        w.writerows(records)
    html_path = out / "latest.html"
    html_path.write_text(render_html(records, meta), encoding="utf-8")
    return out / f"{stem}.json", html_path


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
        parts.append(f'<section class="day{" has-free" if day_free else ""}">'
                     f'<h2>{html.escape(day_label(d))} <span class="cnt">空き {day_free}</span></h2>')
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
        parts.append("</section>")
    # 先頭に置く空き一覧（スマホで最初に見たい情報）
    free_list = []
    for ymd in sorted(tree):
        lines = []
        for v, courts in tree[ymd].items():
            per_court = [f'{html.escape(short_court(c))}: {" ".join(hhmm(r["start"]) for r in slots if r["status"] == "空き")}'
                         for c, slots in courts.items() if any(r["status"] == "空き" for r in slots)]
            if per_court:
                lines.append(f'<div><span class="vn">{html.escape(short_venue(v))}</span> {" ／ ".join(per_court)}</div>')
        if lines:
            d = dt.datetime.strptime(ymd, "%Y%m%d").date()
            free_list.append(f'<li><b>{html.escape(day_label(d))}</b>{"".join(lines)}</li>')
    summary = (f'<details class="summary" open><summary>空き一覧（{meta["free_count"]}コマ）</summary><ul>{"".join(free_list)}</ul></details>'
               if free_list else '<p class="summary">空きはありません。</p>')
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
header {{ position:sticky; top:0; background:var(--bg); padding-bottom:8px; border-bottom:1px solid var(--line); }}
h1 {{ font-size:20px; margin:0 0 4px; }} .meta {{ color:var(--muted); font-size:13px; }}
label {{ display:inline-block; margin-top:8px; font-size:14px; cursor:pointer; }}
h2 {{ font-size:17px; margin:24px 0 8px; }} h3 {{ font-size:14px; margin:12px 0 4px; color:var(--muted); }}
.cnt {{ font-size:13px; font-weight:normal; color:var(--free); }}
.venue {{ overflow-x:auto; }} table {{ border-collapse:collapse; }}
th {{ text-align:left; padding:4px 8px; white-space:nowrap; font-weight:600; font-size:13px; }}
td {{ padding:4px 6px; border:2px solid var(--bg); background:var(--busy-bg); color:var(--muted); font-size:12px; min-width:84px; text-align:center; }}
td .t {{ display:block; }} td .s {{ display:block; font-size:11px; }}
td.free {{ background:var(--free-bg); color:var(--free); font-weight:700; }}
body.only-free section:not(.has-free), body.only-free .venue:not(.has-free), body.only-free tr:not(.has-free), body.only-free td.busy {{ display:none; }}
ul.notes {{ color:var(--muted); font-size:12px; }}
.summary {{ margin:12px 0; font-size:14px; }} .summary summary {{ font-weight:700; cursor:pointer; }}
.summary ul {{ padding-left:18px; margin:6px 0; }} .summary li {{ margin:6px 0; }}
.summary li div {{ margin-left:4px; }} .vn {{ display:inline-block; min-width:3.5em; color:var(--free); font-weight:700; }}
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
<label><input type="checkbox" id="onlyFree"> 空きのみ表示</label>
</header>
{summary}
{body}
<ul class="notes">{notes}</ul>
<p class="meta">出典: ちば施設予約システム（閲覧のみ）。予約は公式サイトから行ってください。</p>
<script>
const cb = document.getElementById('onlyFree');
try {{ cb.checked = localStorage.getItem('onlyFree') === '1'; }} catch (e) {{}}
const apply = () => {{ document.body.classList.toggle('only-free', cb.checked); try {{ localStorage.setItem('onlyFree', cb.checked ? '1' : '0'); }} catch (e) {{}} }};
cb.addEventListener('change', apply); apply();
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
