@echo off
rem 習志野市テニスコート空き状況チェッカー: ダブルクリックで実行し、終わったら latest.html を開く
set PYTHONIOENCODING=cp932
cd /d "%~dp0"
python checker.py --open %*
if errorlevel 1 (
  echo.
  echo エラーが発生しました。上のメッセージを確認してください。
  pause
)
