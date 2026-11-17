@echo off
rem Daily Pricedge scrape, run by the "Pricedge daily scrape" scheduled task.
rem Listings (only the subcategories that are due), price enrichment, the
rem BestPrice market pass and the stock check, pushed to Railway.
rem INGEST_TOKEN comes from the user's environment variables. Output is
rem appended to logs\daily.log.
cd /d "%~dp0"
if not exist logs mkdir logs
echo. >> logs\daily.log
echo ===== %date% %time% ===== >> logs\daily.log
py -X utf8 local_runner.py --plp --enrich-prices --market --check-stock --due --max-minutes 100 >> logs\daily.log 2>&1
