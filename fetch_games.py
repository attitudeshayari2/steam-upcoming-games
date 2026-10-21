#!/usr/bin/env python3
"""
Fetches Steam's "Coming Soon" games and writes games.json.

- Source: Steam store search (filter=comingsoon, games only). This is the storefront's own
  endpoint, not a documented API, so everything is parsed defensively.
- Dates are converted from Steam's text. Anything not understood becomes TBA. Nothing is invented.
- Safety: if Steam fails, the response format looks wrong, or the new list is suspiciously small,
  the script exits with an error and the OLD games.json is left untouched.
"""
import calendar
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# Steam returns "coming soon" games sorted by release date (earliest first, TBA last).
# To cover ALL months/years (not only the next two weeks) we read every page and cap each bucket:
MAX_PER_MONTH = 300       # max games kept for each month that has an exact day or month
MAX_UNDATED_PER_YEAR = 200  # max games kept per year when only a year/quarter/season is known
MAX_TBA = 200             # max games kept with no date (TBA)
MAX_GAMES = 8000          # hard safety limit for the whole file
MAX_PAGES = 500           # hard safety limit for Steam requests
PAGE_SIZE = 50
DELAY = 1.5               # seconds between Steam requests
OUTPUT = "games.json"
BASE = "https://store.steampowered.com/search/results/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def fail(msg):
    print("ERROR: " + msg)
    sys.exit(1)


# ----------------------------------------------------------------------------- dates
def tba():
    return {"p": "tba", "d": None, "e": None, "l": ""}


def valid_year(y):
    return 2000 <= y <= 2100


def month_num(name):
    return MONTHS.get(name[:3], 0)


def make_day(y, mo, d):
    if not mo or not valid_year(y):
        return tba()
    try:
        datetime(y, mo, d)
    except ValueError:
        return tba()
    s = "%04d-%02d-%02d" % (y, mo, d)
    return {"p": "day", "d": s, "e": s, "l": ""}


def month_end(y, mo):
    return "%04d-%02d-%02d" % (y, mo, calendar.monthrange(y, mo)[1])


def parse_date(raw):
    s = re.sub(r"\s+", " ", (raw or "").strip()).rstrip(".").lower()
    if not s or s in ("coming soon", "to be announced", "tba", "tbd", "tbc", "unannounced", "soon"):
        return tba()

    m = re.match(r"^(\d{1,2})\s+([a-z]{3,9})\.?,?\s+(\d{4})$", s)          # 25 Oct, 2026
    if m:
        return make_day(int(m.group(3)), month_num(m.group(2)), int(m.group(1)))
    m = re.match(r"^([a-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})$", s)          # Oct 25, 2026
    if m:
        return make_day(int(m.group(3)), month_num(m.group(1)), int(m.group(2)))
    m = re.match(r"^(spring|summer|fall|autumn|winter|early|mid|late|h1|h2|first half|second half)"
                 r"(?: of)? (\d{4})$", s)                                   # Fall 2026
    if m:
        y = int(m.group(2))
        if not valid_year(y):
            return tba()
        word = m.group(1).upper() if m.group(1) in ("h1", "h2") else m.group(1).title()
        return {"p": "period", "d": "%d-01-01" % y, "e": "%d-12-31" % y, "l": "%s %d" % (word, y)}
    m = re.match(r"^([a-z]{3,9})\.?,?\s+(\d{4})$", s)                       # Oct 2026
    if m:
        y, mo = int(m.group(2)), month_num(m.group(1))
        if not mo or not valid_year(y):
            return tba()
        return {"p": "month", "d": "%04d-%02d-01" % (y, mo), "e": month_end(y, mo), "l": ""}
    m = re.match(r"^q([1-4])\s+(\d{4})$", s)                                # Q4 2026
    if m:
        q, y = int(m.group(1)), int(m.group(2))
        if not valid_year(y):
            return tba()
        start_m = (q - 1) * 3 + 1
        return {"p": "quarter", "d": "%04d-%02d-01" % (y, start_m),
                "e": month_end(y, start_m + 2), "l": "Q%d %d" % (q, y)}
    m = re.match(r"^(\d{4})$", s)                                           # 2027
    if m:
        y = int(m.group(1))
        if not valid_year(y):
            return tba()
        return {"p": "year", "d": "%d-01-01" % y, "e": "%d-12-31" % y, "l": str(y)}
    return tba()


# ------------------------------------------------------------------------ HTML parsing
class ResultParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items = []
        self.cur = None
        self.cap = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = (a.get("class") or "").split()
        if tag == "a" and "search_result_row" in cls:
            self.cur = {"id": (a.get("data-ds-appid") or "").strip(), "title": "", "date": ""}
            self.cap = None
        elif self.cur is not None:
            if tag == "span" and "title" in cls and not self.cur["title"]:
                self.cap = "title"
            elif tag == "div" and "search_released" in cls and not self.cur["date"]:
                self.cap = "date"

    def handle_endtag(self, tag):
        if tag == "a" and self.cur is not None:
            self.items.append(self.cur)
            self.cur = None
            self.cap = None
        elif tag in ("span", "div") and self.cap:
            self.cap = None

    def handle_data(self, data):
        if self.cur is not None and self.cap:
            self.cur[self.cap] += data


def parse_results(html):
    p = ResultParser()
    p.feed(html)
    out = []
    for it in p.items:
        if not it["id"].isdigit():        # skip bundles / packages / multi-app rows
            continue
        title = re.sub(r"\s+", " ", it["title"]).strip()
        if not title:
            continue
        out.append({"id": int(it["id"]), "title": title, "date": it["date"].strip()})
    return out


# ------------------------------------------------------------------------------ Steam
def fetch_page(start):
    params = {
        "query": "", "start": start, "count": PAGE_SIZE, "filter": "comingsoon",
        "category1": 998, "infinite": 1, "cc": "us", "l": "english",
    }
    url = BASE + "?" + urlencode(params)
    for attempt in range(1, 5):
        try:
            with urlopen(Request(url, headers=HEADERS), timeout=30) as r:
                data = json.loads(r.read().decode("utf-8"))
            if not data.get("success") or "results_html" not in data:
                raise ValueError("unexpected response format")
            return data
        except HTTPError as e:
            wait = 60 if e.code in (429, 403) else 5 * attempt
            print("  HTTP %s at start=%d (attempt %d), waiting %ds" % (e.code, start, attempt, wait))
            time.sleep(wait)
        except (URLError, OSError, ValueError) as e:
            print("  error at start=%d (attempt %d): %s" % (start, attempt, e))
            time.sleep(5 * attempt)
    return None


def bucket(g):
    if g["p"] == "tba":
        return "tba"
    if g["p"] in ("day", "month"):
        return g["d"][:7]            # e.g. 2026-11
    return g["d"][:4] + "-x"         # year known, month not


def bucket_limit(key):
    if key == "tba":
        return MAX_TBA
    if key.endswith("-x"):
        return MAX_UNDATED_PER_YEAR
    return MAX_PER_MONTH


def main():
    games, seen, counts = [], set(), {}
    skipped = 0
    start, total, pages = 0, 0, 0
    partial = False

    while pages < MAX_PAGES and len(games) < MAX_GAMES:
        data = fetch_page(start)
        if data is None:
            if len(games) >= 1000:
                print("WARNING: Steam stopped responding at start=%d. Using the %d games collected so far." % (start, len(games)))
                partial = True
                break
            fail("Steam did not respond correctly at start=%d. Old %s kept." % (start, OUTPUT))
        total = int(data.get("total_count") or 0)
        items = parse_results(data["results_html"])
        pages += 1
        if pages % 10 == 1:
            print("start=%d -> %d games parsed (Steam total: %d, kept so far: %d)" % (start, len(items), total, len(games)))

        if not items:
            if start == 0:
                fail("First page parsed 0 games: Steam's page format may have changed. Old %s kept." % OUTPUT)
            break

        for it in items:
            if it["id"] in seen:
                continue
            seen.add(it["id"])
            d = parse_date(it["date"])
            g = {"id": it["id"], "n": it["title"], "p": d["p"], "d": d["d"], "e": d["e"], "l": d["l"]}
            k = bucket(g)
            if counts.get(k, 0) >= bucket_limit(k):
                skipped += 1
                continue
            counts[k] = counts.get(k, 0) + 1
            games.append(g)

        start += PAGE_SIZE
        if total and start >= total:
            break
        if counts.get("tba", 0) >= MAX_TBA:
            break                      # TBA games come last, so everything dated has been read
        time.sleep(DELAY)

    if not games:
        fail("No games collected. Old %s kept." % OUTPUT)

    # Safety: never replace a healthy list with a suspiciously small one.
    if os.path.exists(OUTPUT):
        try:
            with open(OUTPUT, encoding="utf-8") as f:
                old = len(json.load(f).get("games", []))
            if old >= 100 and len(games) < old * 0.5:
                fail("New list (%d) is less than half of the old one (%d). Old %s kept." % (len(games), old, OUTPUT))
        except (ValueError, OSError):
            pass

    stats = {}
    for g in games:
        stats[g["p"]] = stats.get(g["p"], 0) + 1

    payload = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "steam",
        "steam_total": total,
        "count": len(games),
        "partial": partial,
        "games": games,
    }
    tmp = OUTPUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, OUTPUT)

    print("DONE: %d games saved (Steam total %d, %d requests, %d skipped by caps)." % (len(games), total, pages, skipped))
    print("By date type:", stats)
    months = sorted(k for k in counts if k != "tba" and not k.endswith("-x"))
    print("Per month:", ", ".join("%s=%d" % (m, counts[m]) for m in months))
    print("Undated by year:", {k: v for k, v in sorted(counts.items()) if k.endswith("-x")}, "| TBA:", counts.get("tba", 0))
    for g in games[:3]:
        print("  sample:", g["n"], "|", g["p"], g["d"] or "TBA")


if __name__ == "__main__":
    main()
