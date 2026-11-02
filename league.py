"""Who is rostered in the Yahoo league, read from its public pages (no login or API needed)."""
import html
import json
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROSTER_FILE = Path(__file__).with_name("rosters.json")
MAX_AGE = timedelta(hours=24)  # older than this, rosters count as unknown rather than wrong
MIN_ROSTER = 5  # a team page with fewer players means Yahoo changed its pages
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/130.0 Safari/537.36"}
_cache = None


def _page(url):
    res = requests.get(url, headers=HEADERS, timeout=30)
    res.raise_for_status()
    return res.text


def normalize(name):
    """'Nikola Jokić' and 'Nikola Jokic', or 'Kelly Oubre Jr.' and 'Kelly Oubre', compare equal."""
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    plain = re.sub(r"[.'’-]", "", plain)
    return " ".join(w for w in plain.split() if w not in {"jr", "sr", "ii", "iii", "iv"})


def fetch_owners(league_id):
    """{normalized player name: fantasy team name} for every rostered player. Raises if a page looks wrong."""
    base = f"https://basketball.fantasysports.yahoo.com/nba/{league_id}"
    teams = {}
    for tid, name in re.findall(rf'href="[^"]*/nba/{league_id}/(\d{{1,2}})"[^>]*>\s*([^<]+?)\s*<', _page(base)):
        teams.setdefault(tid, html.unescape(name))
    if len(teams) < 2:
        raise ValueError(f"Found {len(teams)} teams on the league page")
    owners = {}
    for tid, team in teams.items():
        time.sleep(1)
        # The title attribute has the full name; the link text shortens some, like "N. Alexander-Walker".
        players = re.findall(r'ysf-player-name[^>]*>\s*<a[^>]*title="([^"]+)"', _page(f"{base}/{tid}"))
        if len(players) < MIN_ROSTER:
            raise ValueError(f"Found {len(players)} players on {team}")
        owners.update({normalize(html.unescape(p)): team for p in players})
    return owners


def _load():
    global _cache
    if _cache is None:
        _cache = json.loads(ROSTER_FILE.read_text(encoding="utf-8")) if ROSTER_FILE.exists() else {}
    return _cache


def age():
    """How old the saved rosters are, or None if there are none."""
    fetched = _load().get("fetched_at")
    return datetime.now(timezone.utc) - datetime.fromisoformat(fetched) if fetched else None


def known():
    return age() is not None and age() < MAX_AGE


def refresh_if_stale(league_id, hours):
    """Re-read the league every `hours`. A failure keeps the saved rosters and raises for the caller to log."""
    global _cache
    if not league_id or (age() is not None and age() < timedelta(hours=hours)):
        return
    _cache = {"fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "owners": fetch_owners(league_id)}
    ROSTER_FILE.write_text(json.dumps(_cache, indent=1, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def owner(name):
    """The fantasy team he's on, '' if nobody rosters him, None if rosters are unknown."""
    return _load()["owners"].get(normalize(name), "") if known() else None
