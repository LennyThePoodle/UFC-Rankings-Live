"""
Scrape ufc.com/rankings and append today's snapshot to rankings_current.csv.

Schema matches martj42/ufc_rankings_history: date,weightclass,fighter,rank
(rank 0 = champion), so the two files concatenate cleanly.

Idempotent: if today's date is already in the CSV, it exits without writing.
Safe to run more than once a day, and safe to re-run after a failure.

IMPORTANT -- the SELECTORS below are unverified. ufc.com's markup changes and
the Meta rankings rollout (2026-06-20) altered the page. Run with --dry-run
first and confirm the row count and division names look right before you let
the scheduled job commit anything.
"""

import argparse
import csv
import os
import sys
from datetime import date

import requests
from bs4 import BeautifulSoup

URL = "https://www.ufc.com/rankings"
CSV_PATH = "rankings_current.csv"
HEADER = ["date", "weightclass", "fighter", "rank"]

# A real UA matters -- the default requests UA gets blocked.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def fetch(url=URL):
    r = requests.get(url, headers=HEADERS, timeout=60)
    r.raise_for_status()
    return r.text


def parse(html, snapshot_date):
    """Return list of (date, division, fighter, rank) tuples."""
    soup = BeautifulSoup(html, "html.parser")
    rows = []

    for group in soup.select("div.view-grouping"):
        head = group.select_one("div.view-grouping-header")
        if not head:
            continue
        division = head.get_text(strip=True)

        champ = group.select_one("div.rankings--athlete--champion h5")
        if champ:
            rows.append((snapshot_date, division, champ.get_text(strip=True), 0))

        for tr in group.select("table tbody tr"):
            cells = tr.find_all("td")
            if len(cells) < 2:
                continue
            rank_txt = cells[0].get_text(strip=True)
            name = cells[1].get_text(strip=True)
            if rank_txt.isdigit() and name:
                rows.append((snapshot_date, division, name, int(rank_txt)))

    return rows


def existing_dates(path):
    if not os.path.exists(path):
        return set()
    with open(path, newline="", encoding="utf-8") as f:
        return {r["date"] for r in csv.DictReader(f)}


def append(path, rows):
    is_new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(HEADER)
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="parse and report, but do not write the CSV")
    args = ap.parse_args()

    today = date.today().isoformat()

    if not args.dry_run and today in existing_dates(CSV_PATH):
        print(f"{today} already present -- nothing to do")
        return 0

    rows = parse(fetch(), today)

    if not rows:
        # Fail loudly. A silent zero-row run would look like "no changes"
        # in the workflow and you would not notice the parser had broken.
        print("ERROR: parsed 0 rows -- selectors are stale or the page is blocked",
              file=sys.stderr)
        return 1

    divisions = sorted({r[1] for r in rows})
    print(f"{today}: {len(rows)} rows across {len(divisions)} divisions")
    for d in divisions:
        print(f"  {d}: {sum(1 for r in rows if r[1] == d)}")

    if args.dry_run:
        print("\n--dry-run: not writing")
        return 0

    append(CSV_PATH, rows)
    print(f"appended to {CSV_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
