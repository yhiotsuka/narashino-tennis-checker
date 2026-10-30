# ちば施設予約システム（習志野市 U4）テニスコート空き状況 調査メモ

調査日: 2026-10-30 / 実施環境: Windows 11, Python 3.12.10, Playwright (Chromium headless) を今回インストール済み
アクセス数: 合計 約52ページ遷移（各遷移 3秒以上待機）。ログイン・予約・カート操作は一切なし。

## 結論（要点）

- **ログイン不要**で空き状況を閲覧できる（SP版・PC版とも）。「ログインして予約」ボタンより先には進んでいない。
- サーバーは Struts 系（`*.do`）で**セッション + 画面遷移状態を保持**する。途中画面のURLに直接アクセスすると
  「セッションタイムアウト」または「遷移情報に誤りがあります」エラーになる → **必ずトップから順に辿る**必要あり。
- **PC版 (`/w/`) がスクレイピングに最適**:
  1館 × 1日 × 最大5コート を 1ページで取得でき、施設チェックボックスでテニスコートだけに絞れる。
  日付変更は hidden 項目を書き換えて POST するだけ。状態は `<img alt="空き|予約|保守…">` で判定可能。
- SP版は 1コート × 1日 / ページ なので、網羅チェックには非効率（ページ数が6倍以上）。
- プロトタイプ `scripts/fetch_pc.py` で PC版フローをヘッドレス実行し、取得できることを確認済み。

## 対象施設・コード一覧

| 館 (bldCd) | テニスコート (instCd) | 時間帯（調査時点） |
|---|---|---|
| 秋津総合運動公園 U4020 | A U4020010 / B 020 / C 030 / D 040 / E 050 / F 060 | A–C: 9–11,11–13,13–15,15–17,17–19,19–21（ナイター有）／D・E: 9時〜15–16:30の4枠（ナイター無）／F 未確認 |
| 芝園公園 U4030 | A U4030010 / B 020 / C 030 / D 040 | 9〜21時の2時間×6枠 |
| 袖ケ浦運動公園 U4040 | A U4040070 / B 080 / C 090 / D 100 | 未確認（同館は体育館等と混在、テニスは6件目以降） |
| 実籾テニスコート U4070 | A U4070010 〜 F U4070060 | 平日 9–11,11–13,13–15,15–17 の4枠／**祝日(11/3)は 8:30–10:30,…,14:30–16:30** |

※ 時間帯は日付・曜日で変わるので、毎回 `selectInfo` の start/end を読むこと（固定値にしない）。
※ 同館の非テニス施設（参考）: 秋津=第一カッター球場/多目的広場/研修室/カッターフィールド、芝園=フットサルA–C（1時間枠）、袖ケ浦=体育館各面/少年サッカー場。

## PC版 (https://www.cm1.eprs.jp/yoyaku-chiba/w/) 遷移手順

すべて `form.action = <URL>; form.submit()` による **POST**（`doAction(form, action)` 関数）。

| # | 画面 (displayNo) | 操作 | POST先 |
|---|---|---|---|
| 1 | ホーム (pauab1000) | GET `/yoyaku-chiba/w/` | — |
| 2 | 施設名検索 (pruaa1000) | `doAction(document.form1, gRsvUTransNameSearchAction)` | `/w/rsvUTransNameSearchAction.do` |
| 3 | 施設名検索結果 (pruaa2000) | childForm: `checkCommunityCd=U4` をチェック、`textKeyword=テニス` → `doNameSearch(childForm, gRsvUNameSearchAction)` | `/w/rsvUNameSearchAction.do` |
| 4 | 空き状況 (pruca1000) | `doSelect(childForm, gRsvUInstSrchVacantAction, 'U4020')` ※館コード | `/w/rsvUInstSrchVacantAction.do` |
| 5 | 空き状況（日付/施設変更） | form1 の `chkIcd` チェックボックスでテニスだけ残し、`#year/#month/#day` (= `dispYY/dispMM/dispDD`) を設定 → `doReloadCheck(form1, gRsvUInstSrchVacantAction)` | 同上 |
| 6 | ページ送り（5件超） | `movePage(n)` （`pageNo=n`, `opeFlg=1`） | 同上 |
| 7 | 別の館へ | `doAction(document.rsvMenuform, gRsvUTransNameSearchAction)` で手順2へ戻り 3→4 を再実行（約3遷移/館） | — |

日付変更（カレンダークリック）は `selectCalendarDate(y,m,d)` = `#year,#month,#day` をセットして同じ POST。
週表示/月表示のアクション（`rsvUInstSrchWeekVacantAction.do` / `rsvUInstSrchMonthVacantAction.do`）がJS定数に存在するが、
空き状況画面から form1 で呼ぶと「システムエラー」。画面上にボタンも無いため**使わない**。

### 空き状況画面 form1 の主な項目（POST, action は JS で設定）

```
forward=pruca1000, displayNo=pruca1000
paramBcd=U4020, paramIcd=U4020010
dispYY=2026, dispMM=11, dispDD=1          (#year,#month,#day)
pageNo=1, opeFlg=0|1
chkIcd=<instCd> (checkbox, 館内全施設分)   → doReloadCheck が checkIcd[] / checkIcdNum に詰め替え
checkIcdNum=N, checkIcd=<instCd> × 館内施設数
selectInfo / checkInfo  (各コマの hidden。selectInfo は disabled なので送信されない)
communityCd=
```

### 空き状況表のHTML構造（pruca1000）

コート1面ごとに `table.tablebg2`:

```html
<table class="tablebg2">
  <tr><td id="blabel" colspan="6"> ... <font class="font-navy">●テニスコートＡ</font> ... [予約カートに追加]ボタン</td></tr>
  <tr><td class="time-table1">９時～</td> ...（時間帯見出し）</tr>
  <tr>
    <td class="time-table2"><div>
      <img src="image/lw_rsving.gif" alt="予約">
      <input type="hidden" name="selectInfo" value="U4020:U4020010:20261030:00010:0900:1100:U40020:1:0" disabled>
      <input type="hidden" name="checkInfo" value="0">
    </div></td> ...
  </tr>
</table>
```

- `selectInfo` = `館:施設:YYYYMMDD:時間帯No:開始HHMM:終了HHMM:(内部コード):…` → 日付・時間帯をここから確実に取れる。
- 状態は `img@alt`（`img@src` でも可）:

| alt | src | 意味 |
|---|---|---|
| 空き | lw_rsvok.gif | 空き（クリック可） |
| 予約 | lw_rsving.gif | 予約済 |
| 保守 | lw_mtnstatus.gif | 保守 |
| 休館 | lw_closestatus.gif | 休館 |
| 一般開放 | lw_openstatus.gif | 一般開放 |
| 雨天 | lw_rainstatus.gif | 雨天 |
| 受付期間外 | lw_outstatus.gif | 受付期間外 |
| 時間帯なし | lw_nonestatus.gif | 時間帯なし |
| カート追加選択中/済/不可 | lw_cart / lw_rcart / lw_starstatus | カート関連（閲覧のみなら出ない） |

- 件数表示: `1 ～ 5 件表示 （ 6 件中 ）`。1ページ最大5施設。

### 網羅チェックの必要ページ数（PC版の見積り）

テニス面数: 秋津6・芝園4・袖ケ浦4・実籾6 → 1日あたり 2+1+1+2 = **6ページ**（+館切替 約3×4）。
例: 14日分 ≒ 84 + 12 ≒ 100 遷移（3秒間隔で約5〜6分）。受付期間（何日先まで「受付期間外」でないか）は未調査。

## SP版 (https://www.cm1.eprs.jp/yoyaku-chiba/sp/)

- **スマホUA必須**（PC UA だと「このブラウザは未対応です」）。Playwright なら `devices['Pixel 5']` 等を使う。
- 指定URL `rsvPrpia1000NextAction.do?displayNo=prpia1000&selectedCommunityCd=U4` は**直接開くと「セッションタイムアウト」**。
  トップから辿れば同URLが有効。
- 遷移はすべて GET リンク（施設名検索のみ POST）:
  1. `/sp/` （認証前メニュー）
  2. `/sp/rsvPTransInstSrchVacantAction.do?displayNo=papab1000` （空き検索TOP）
  3. `/sp/rsvPrpia1000DispAction.do?displayNo=prpaa1000&conditionMode=2` （施設から→自治体選択）
  4. `/sp/rsvPrpia1000NextAction.do?displayNo=prpia1000&selectedCommunityCd=U4` （施設名入力）
  5. POST `/sp/rsvPInstSrachInstnameBludAction.do` `srchInstName=テニス&displayNo=prpab3000`（空欄不可・5文字まで）
  6. `/sp/rsvPTransInstSrchInstAction.do?displayNo=prpac1000&selectBldCd=U4020` （館→施設一覧、10件/頁）
  7. `/sp/rsvPTransInstSrchDayWeekAction.do?displayNo=prpad1000&selectInstCd=U4020010&selectInstLendFlg=1` （期間設定）
  8. POST `/sp/rsvPTransInstSrchVacantTzoneAction.do` `selectMonth=11&selectDay=01&srchSelectWeek=1..8&displayNo=prpae1000`（月日必須）
  9. 結果 (prpaf1000): 1コート1日。`前日/翌日` = `rsvPTransInstSrchVacantTzonePageMoveAction.do?displayNo=prpaf1000&pageMode=2|1&ccc=NN`
- 結果の構造: `<li>９時～&nbsp;<span class="ff-monospace">×</span></li>`。記号は × を確認（○等は未確認）。
- 現在画面と異なる `displayNo` のURLへ飛ぶと「遷移情報に誤りがあります」→ 戻りリンクを辿る必要あり。

## その他

- 簡易版 `https://www.cm1.eprs.jp/yoyaku-chiba/ew/` もある（未調査）。
- ページ文字コードは Shift_JIS。
- サイト告知: 携帯電話（ガラケー）画面は 2026/3/31 で終了済み。SP版・簡易版は継続。

## ファイル

- `scripts/fetch_pc.py` … PC版で 館コード＋日付 を指定し、テニスコートの全コマを JSON 出力（`python fetch_pc.py U4070 20261103 ../samples`）。5件超の館（秋津・実籾）の2ページ目取得は未実装（`movePage(2)` を追加すればよい）。
- `scripts/probe1.py` … 最初の疎通確認用。
- `samples/pc_01〜05_*.html` … PC版各画面（ホーム→施設名検索→検索結果→実籾 当日→実籾 11/3 テニスのみ）
- `samples/sp_vacant_U4020010_20261101.html` … SP版 空き状況結果（秋津A 11/1、DOMから整形保存）
- `samples/sp_error_direct_link_session_timeout.html` … SP版URL直接アクセス時のエラー
