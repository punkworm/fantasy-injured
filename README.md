# Fantasy Injured 🚑

Telegram bot ([@nbainjured_bot](https://t.me/nbainjured_bot)) for NBA fantasy injury news. Every few minutes it checks
ESPN's public NBA injury report. When a fantasy-relevant player (≥ `MIN_OWNED`% owned, or on your `MY_PLAYERS` list)
is newly listed **Out**, it messages you with:

- the injury, expected return date and ESPN's news write-up (which often names who benefits)
- **who steps up**: the healthy players right behind him on his team's ESPN depth chart, with minutes/PTS/REB/AST
  and ESPN % owned (🟢 under 50% means probably on waivers)

It also sends news updates while he's out (`NEWS_UPDATES`) and a message when he's back (`ALERT_ON_RETURN`).

## Setup

```bash
pip install -r requirements.txt
```

`.env` already has the Telegram token and chat id. Optional: put your Yahoo roster in `MY_PLAYERS` (comma separated).

```bash
python bot.py test              # sample alert for the most-owned player currently out
python bot.py test "curry"      # sample alert for a specific injured player
python bot.py                   # run (or double-click start_bot.bat)
```

The first run only records the current injury report, so you only get alerts for changes after that.
The bot has to stay running for alerts to arrive.

## Yahoo (on hold)

`yahoo.py` can read your actual Yahoo league (rosters, real free agents), but since July 2026 Yahoo returns
`403 This application is not authorized` until an app is approved through their access program. Once the
app in `.env` (`YAHOO_CLIENT_ID`) is approved, it can replace the ESPN "% owned" guess with your league's real free agents.

## Files

| File | |
|---|---|
| `bot.py` | polling loop, fill-in ranking, Telegram messages |
| `espn.py` | ESPN injuries, depth charts, player averages / % owned |
| `yahoo.py` | Yahoo OAuth2 + league endpoints (waiting on Yahoo approval) |
| `.env` | config and secrets (git-ignored) |
| `state.json`, `tokens.json` | created at runtime (git-ignored) |
