@echo off
rem Daily Pricedge scrape, run by the "Pricedge daily scrape" scheduled task.
rem Listings (only the subcategories that are due), price enrichment and the
rem stock check, pushed to Railway. INGEST_TOKEN comes from the user's
rem environment variables. Output is appended to logs\daily.log.
cd /d "%~dp0"
if not exist logs mkdir logs
echo. >> logs\daily.log
echo ===== %date% %time% ===== >> logs\daily.log
py -X utf8 local_runner.py --plp --enrich-prices --check-stock --due --max-minutes 75 >> logs\daily.log 2>&1
