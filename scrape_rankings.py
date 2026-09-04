"""
Scrape ufc.com/rankings and append today's snapshot to rankings_current.csv.

Scopes to the "All Rankings" section only. Since 2026-06-20 the page also
renders a "Meta Rankings" section (Elo-based) using the same
div.view-grouping markup, which is why an unscoped selector returned every
weight division twice. Both sections live under distinct containers:

    div.view-display-id-block_1        <- All Rankings  (what we want)
    div.view-display-id-meta_rankings  <- Meta Rankings (ignored)

The block_1 container holds 13 groupings in both the pre- and post-6/20
formats, so this selector works either side of the change.

Schema matches martj42/ufc_rankings_history: date,weightclass,fighter,rank
  - rank 0 = divisional champion
  - pound-for-pound has NO rank 0. The page's P4P "champion" slot just
    repeats the #1 fighter (its header reads "...Pound-for-PoundTop Rank"),
    and martj42 stores no rank 0 for P4P either.

Rank repair: UFC's own HTML sometimes emits a duplicate rank with the next
integer missing -- e.g. 2026-06-17 Women's Strawweight listed 1,2,3,4,5,6,6,8.
Where ranks are non-decreasing and positional renumbering yields a clean 1..N,
we renumber and log it. Anything less clear-cut is dropped, not guessed.

Idempotent: exits without writing if today's date is already in the CSV.
"""

import argparse
import csv
import os
import re
import sys
from datetime import date

import requests
from bs4 import BeautifulSoup

URL = "https://www.ufc.com/rankings"
CSV_PATH = "rankings_current.csv"
HEADER = ["date", "weightclass", "fighter", "rank"]

ALL_RANKINGS = "div.view-display-id-block_1"
META_RANKINGS = "div.view-display-id-meta_rankings"

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


def clean(s):
    return " ".join(s.split())


def division_name(text):
    """'Men's Pound-for-PoundTop Rank' -> 'Men's Pound-for-Pound'."""
    return re.sub(r"\s*Top Rank$", "", clean(text))


def is_p4p(division):
    return division.endswith("Pound-for-Pound")


def parse(html, snapshot_date):
    """Return rows from the All Rankings section, or None if it's missing."""
    soup = BeautifulSoup(html, "html.parser")

    root = soup.select_one(ALL_RANKINGS)
    if root is None:
        # Refuse rather than falling back to the whole document, which would
        # sweep Meta Rankings back in and reintroduce the duplication.
        return None

    rows = []
    for group in root.select("div.view-grouping"):
        head = group.select_one("div.view-grouping-header")
        if not head:
            continue
        division = division_name(head.get_text(strip=True))

        if not is_p4p(division):
            champ = group.select_one("div.rankings--athlete--champion h5")
            if champ:
                rows.append((snapshot_date, division, clean(champ.get_text()), 0))

        for tr in group.select("table tbody tr"):
            cells = tr.find_all("td")
            if len(cells) < 2:
                continue
            rank_txt = cells[0].get_text(strip=True)
            name = clean(cells[1].get_text())
            if rank_txt.isdigit() and name:
                rows.append((snapshot_date, division, name, int(rank_txt)))

    return rows


def clean_blocks(rows):
    """Validate and repair per division. Returns (good_rows, notes, dropped)."""
    blocks = {}
    for r in rows:
        blocks.setdefault((r[0], r[1]), []).append(r)

    good, notes, dropped = [], [], []

    for (snapshot_date, division), block in blocks.items():
        champs = [r for r in block if r[3] == 0]
        contenders = [r for r in block if r[3] > 0]
        ranks = [r[3] for r in contenders]
        names = [r[2] for r in block]

        if len(set(names)) != len(names):
            dupes = sorted({n for n in names if names.count(n) > 1})
            dropped.append((division, f"duplicate fighter(s): {dupes}"))
            continue

        if not 5 <= len(contenders) <= 15:
            dropped.append((division, f"{len(contenders)} contenders -- unexpected"))
            continue

        if ranks == list(range(1, len(ranks) + 1)):
            good.extend(block)
            continue

        if any(b < a for a, b in zip(ranks, ranks[1:])):
            dropped.append((division, f"ranks not ascending: {ranks}"))
            continue

        fixed = [(r[0], r[1], r[2], i) for i, r in enumerate(contenders, start=1)]
        changed = [(o[2], o[3], n[3]) for o, n in zip(contenders, fixed) if o[3] != n[3]]

        if len(changed) > 3:
            dropped.append((division, f"{len(changed)} positions would move -- not trusted"))
            continue

        notes.append((division, "; ".join(f"{nm}: {o}->{n}" for nm, o, n in changed)))
        good.extend(champs + fixed)

    return good, notes, dropped


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
        w.writerows(sorted(rows))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="parse and report, but do not write the CSV")
    ap.add_argument("--from-file", metavar="PATH",
                    help="parse a saved HTML file instead of fetching")
    args = ap.parse_args()

    today = date.today().isoformat()

    if not args.dry_run and not args.from_file and today in existing_dates(CSV_PATH):
        print(f"{today} already present -- nothing to do")
        return 0

    if args.from_file:
        with open(args.from_file, encoding="utf-8") as f:
            html = f.read()
    else:
        html = fetch()

    has_meta = bool(BeautifulSoup(html, "html.parser").select_one(META_RANKINGS))
    print(f"page format: {'post-6/20 (meta section present, excluded)' if has_meta else 'pre-6/20'}")

    rows = parse(html, today)
    if rows is None:
        print("ERROR: no All Rankings container found -- page structure changed",
              file=sys.stderr)
        return 1
    if not rows:
        print("ERROR: parsed 0 rows -- selectors are stale or the page is blocked",
              file=sys.stderr)
        return 1

    good, notes, dropped = clean_blocks(rows)

    for division in sorted({r[1] for r in good}):
        n = sum(1 for r in good if r[1] == division)
        print(f"  {division}: {n}")
    print(f"\n{today}: {len(good)} rows across {len({r[1] for r in good})} divisions")

    for division, note in sorted(notes):
        print(f"  REPAIRED {division}: {note}")
    for division, reason in sorted(dropped):
        print(f"  DROPPED  {division}: {reason}", file=sys.stderr)

    if not good:
        print("ERROR: nothing survived validation", file=sys.stderr)
        return 1

    if args.dry_run or args.from_file:
        print("\nnot writing (dry-run or --from-file)")
        return 0

    append(CSV_PATH, good)
    print(f"appended to {CSV_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
