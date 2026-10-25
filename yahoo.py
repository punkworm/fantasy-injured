"""Minimal Yahoo Fantasy Sports API client (OAuth2 + the few endpoints the bot needs)."""
import base64
import json
import os
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

AUTH_URL = "https://api.login.yahoo.com/oauth2/request_auth"
TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
API_BASE = "https://fantasysports.yahooapis.com/fantasy/v2"
TOKEN_FILE = Path(__file__).with_name("tokens.json")


class YahooClient:
    def __init__(self):
        self.client_id = os.environ["YAHOO_CLIENT_ID"]
        self.client_secret = os.environ["YAHOO_CLIENT_SECRET"]
        self.redirect_uri = os.environ.get("YAHOO_REDIRECT_URI", "https://localhost:3000/")
        self.tokens = json.loads(TOKEN_FILE.read_text()) if TOKEN_FILE.exists() else None

    # ── OAuth ────────────────────────────────────────────────────────────────
    def auth_url(self):
        return AUTH_URL + "?" + urlencode({
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": "fspt-r",  # Fantasy Sports read access; without it the API returns 403
        })

    def exchange_code(self, code_or_url):
        code = code_or_url.strip()
        if code.startswith("http"):
            code = parse_qs(urlparse(code).query)["code"][0]
        self._token_request({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
        })

    def _refresh(self):
        self._token_request({
            "grant_type": "refresh_token",
            "refresh_token": self.tokens["refresh_token"],
            "redirect_uri": self.redirect_uri,
        })

    def _token_request(self, data):
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        res = requests.post(TOKEN_URL, data=data, timeout=30, headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        })
        res.raise_for_status()
        tokens = res.json()
        # Yahoo doesn't always resend the refresh token on refresh; keep the old one.
        if self.tokens and "refresh_token" not in tokens:
            tokens["refresh_token"] = self.tokens["refresh_token"]
        tokens["expires_at"] = time.time() + int(tokens.get("expires_in", 3600)) - 120
        self.tokens = tokens
        TOKEN_FILE.write_text(json.dumps(tokens, indent=2))

    # ── API ──────────────────────────────────────────────────────────────────
    def get(self, path):
        if not self.tokens:
            raise RuntimeError("Not authorized yet. Run: python bot.py auth")
        if time.time() > self.tokens["expires_at"]:
            self._refresh()
        url = f"{API_BASE}/{path}{'&' if '?' in path else '?'}format=json"
        for attempt in range(2):
            res = requests.get(url, timeout=30,
                               headers={"Authorization": f"Bearer {self.tokens['access_token']}"})
            if res.status_code == 401 and attempt == 0:
                self._refresh()
                continue
            res.raise_for_status()
            return res.json()["fantasy_content"]

    def league_key(self, league_id):
        game_key = self.get("game/nba")["game"][0]["game_key"]
        return f"{game_key}.l.{league_id}"

    def stat_names(self, league_key):
        """stat_id -> display name (e.g. '12' -> 'PTS') for this league."""
        settings = self.get(f"league/{league_key}/settings")["league"][1]["settings"][0]
        return {str(s["stat"]["stat_id"]): s["stat"]["display_name"]
                for s in settings["stat_categories"]["stats"]}

    def rostered_players(self, league_key):
        """All players on any fantasy roster, with the owning fantasy team attached."""
        teams = self.get(f"league/{league_key}/teams/roster")["league"][1]["teams"]
        players = []
        for team_entry in collection(teams, "team"):
            team_info = merge(team_entry[0])
            roster = team_entry[1]["roster"]["0"]["players"]
            for p in collection(roster, "player"):
                info = parse_player(p)
                info["fantasy_team"] = team_info.get("name", "?")
                info["is_mine"] = str(team_info.get("is_owned_by_current_login", "0")) == "1"
                players.append(info)
        return players

    def available_players(self, league_key, limit=150):
        """Top available (FA + waivers) players by last-30-day rank, with last-30-day stats."""
        players = []
        for start in range(0, limit, 25):
            data = self.get(
                f"league/{league_key}/players;status=A;sort=AR;sort_type=lastmonth;"
                f"start={start};count=25/stats;type=lastmonth"
            )
            page = data["league"][1].get("players")
            if not page:  # Yahoo returns [] when there are no more results
                break
            for p in collection(page, "player"):
                info = parse_player(p)
                info["avail_rank"] = len(players) + 1
                players.append(info)
        return players


# ── Yahoo JSON helpers ───────────────────────────────────────────────────────
# Yahoo's JSON is a mix of {"count": n, "0": {...}, "1": {...}} collections and
# lists of single-key dicts, so these flatten it into plain dicts.

def collection(obj, kind):
    if not isinstance(obj, dict):
        return []
    return [obj[k][kind] for k in obj if k.isdigit() and kind in obj[k]]


def merge(items):
    out = {}
    for item in items:
        if isinstance(item, dict):
            out.update(item)
    return out


def parse_player(p):
    info = merge(p[0])
    extras = merge(p[1:])
    stats = {}
    for s in (extras.get("player_stats") or {}).get("stats", []):
        stats[str(s["stat"]["stat_id"])] = s["stat"]["value"]
    return {
        "key": info["player_key"],
        "name": info.get("name", {}).get("full", "?"),
        "nba_team": info.get("editorial_team_abbr", "?").upper(),
        "positions": info.get("display_position", ""),
        "status": info.get("status", ""),
        "status_full": info.get("status_full", ""),
        "injury_note": info.get("injury_note", ""),
        "stats": stats,
    }
