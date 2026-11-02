"""ESPN's public (no key needed) NBA injury, depth chart and player endpoints."""
import re
from datetime import datetime

import requests

SITE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
ATHLETE = "https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/athletes"


def _get(url):
    res = requests.get(url, timeout=30)
    res.raise_for_status()
    return res.json()


def _athlete_id(athlete):
    if athlete.get("id"):
        return str(athlete["id"])
    for link in athlete.get("links", []):
        m = re.search(r"/id/(\d+)", link.get("href", ""))
        if m:
            return m.group(1)
    return None


def injuries():
    """Every player on ESPN's NBA injury report, keyed by ESPN athlete id."""
    out = {}
    for team in _get(f"{SITE}/injuries")["injuries"]:
        for inj in team["injuries"]:
            a = inj["athlete"]
            aid = _athlete_id(a)
            if not aid:
                continue
            details = inj.get("details") or {}
            out[aid] = {
                "id": aid,
                "name": a["displayName"],
                "team_id": str(a.get("team", {}).get("id") or team["id"]),
                "team": a.get("team", {}).get("abbreviation", "?"),
                "position": (a.get("position") or {}).get("abbreviation", ""),
                "status": inj["status"],  # "Out", "Day-To-Day", ...
                "short": inj.get("shortComment", ""),
                "long": inj.get("longComment", ""),
                "injury": " ".join(filter(None, [details.get("side"), details.get("type"), details.get("detail")])),
                "return_date": details.get("returnDate", ""),
            }
    return out


def depth_chart(team_id):
    """{'pg': [{'id', 'name', 'out'}...], 'sg': [...], ...} in depth order."""
    chart = _get(f"{SITE}/teams/{team_id}/depthcharts")["depthchart"][0]["positions"]
    return {
        pos: [{"id": str(a["id"]), "name": a["displayName"],
               "out": any(i.get("status") == "Out" for i in a.get("injuries", []))}
              for a in slot["athletes"]]
        for pos, slot in chart.items()
    }


def player_overview(athlete_id):
    """Season averages and ESPN % owned for one player (missing values -> None)."""
    o = _get(f"{ATHLETE}/{athlete_id}/overview")
    info = {"owned": None, "gp": None, "min": None, "pts": None, "reb": None, "ast": None}
    try:
        info["owned"] = float(o["fantasy"]["percentOwned"])
    except (KeyError, TypeError, ValueError):
        pass
    stats = o.get("statistics") or {}
    names = stats.get("names", [])
    season = next((s for s in stats.get("splits", []) if s.get("displayName") == "Regular Season"), None)
    if season:
        row = dict(zip(names, season["stats"]))
        for key, name in [("gp", "gamesPlayed"), ("min", "avgMinutes"), ("pts", "avgPoints"),
                          ("reb", "avgRebounds"), ("ast", "avgAssists")]:
            try:
                info[key] = float(row[name])
            except (KeyError, ValueError):
                pass
    return info


def upcoming_games(team_id):
    """The team's unplayed games in date order: [{'date', 'opponent', 'home'}...]. [] if unavailable."""
    try:
        games = []
        for event in _get(f"{SITE}/teams/{team_id}/schedule")["events"]:
            comp = event["competitions"][0]
            if comp["status"]["type"]["state"] != "pre":
                continue
            us = next(t for t in comp["competitors"] if str(t["team"]["id"]) == str(team_id))
            them = next(t for t in comp["competitors"] if t is not us)
            games.append({"date": datetime.fromisoformat(event["date"]),
                          "opponent": them["team"]["abbreviation"],
                          "home": us["homeAway"] == "home"})
        return sorted(games, key=lambda g: g["date"])
    except (requests.RequestException, KeyError, IndexError, StopIteration, ValueError):
        return []


def recent_minutes(athlete_id, games=5):
    """Average minutes over his last regular season games: {'min', 'games'}. None if unavailable."""
    try:
        log = _get(f"{ATHLETE}/{athlete_id}/gamelog")
        col = log["names"].index("minutes")
        played = [e for season in log["seasonTypes"] if "Regular Season" in season.get("displayName", "")
                  for cat in season.get("categories", []) if cat.get("type") == "event"
                  for e in cat.get("events", [])]
        played.sort(key=lambda e: log["events"][e["eventId"]]["gameDate"], reverse=True)
        minutes = [float(e["stats"][col]) for e in played[:games]]
    except (requests.RequestException, KeyError, IndexError, ValueError):
        return None
    return {"min": sum(minutes) / len(minutes), "games": len(minutes)} if minutes else None
