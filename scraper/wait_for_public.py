#!/usr/bin/env python3
"""Wait for Public's bot block (403) to lift, then take Public's prices once.

Every 30 minutes it sends ONE request to a Public listing page. When Public
answers normally it runs a prices-only Public scrape (listing pages only: no
price enrichment, no stock check - the lightest possible run) and exits. It
gives up at 08:00 so it never overlaps the 08:30 daily scheduled run.

    py -X utf8 wait_for_public.py          (INGEST_TOKEN must be set)
Log: logs/public_watch.log
"""
import datetime as dt
import os
import subprocess
import sys
import time

import requests

import public_plp

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "logs", "public_watch.log")
PROBE = public_plp.BASE
PARAMS = {"s": "/cat/oikiakes-syskeyes/psygeia/psygeiokatapsiktes/", "p": 1,
          "getFilters": "false", "locale": "el"}
EVERY = 30 * 60


def log(msg):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(f"[{dt.datetime.now():%Y-%m-%d %H:%M}] {msg}\n")


def main():
    now = dt.datetime.now()
    give_up = (now + dt.timedelta(days=1 if now.hour >= 8 else 0)).replace(hour=8, minute=0, second=0, microsecond=0)
    log(f"watching Public every {EVERY // 60} min until {give_up:%a %H:%M}")
    while dt.datetime.now() < give_up:
        try:
            code = requests.get(PROBE, params=PARAMS, headers=public_plp.HEADERS, timeout=20).status_code
        except Exception as e:
            code = f"error {str(e)[:60]}"
        log(f"Public answered {code}")
        if code == 200:
            log("block lifted - running a prices-only Public scrape")
            with open(LOG, "a", encoding="utf-8") as fh:
                rc = subprocess.call([sys.executable, "-X", "utf8", "local_runner.py",
                                      "--site", "public", "--plp", "--max-minutes", "60"],
                                     cwd=HERE, stdout=fh, stderr=subprocess.STDOUT)
            log(f"Public prices done (exit code {rc})")
            return
        time.sleep(EVERY)
    log("still blocked at the cut-off - leaving it to the 08:30 daily run")


if __name__ == "__main__":
    main()
