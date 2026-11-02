"""NBA injury alert bot -> Telegram, with depth-chart fill-in suggestions (ESPN data).

Usage:
  python bot.py chatid          find your Telegram chat id (message the bot first)
  python bot.py test [name]     send a sample alert for a player on the injury report
  python bot.py digest          print today's pre-game digest without sending it
  python bot.py once            run a single check (for cron / GitHub Actions)
  python bot.py                 run forever, polling every POLL_MINUTES
"""
import html
import json
import os
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

import espn

load_dotenv(Path(__file__).with_name(".env"))
sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to a non-UTF-8 codepage

STATE_FILE = Path(__file__).with_name("state.json")
DIGEST_FILE = Path(__file__).with_name("digest.json")


def env_list(name, default=""):
    return [s.strip() for s in os.environ.get(name, default).split(",") if s.strip()]


ALERT_STATUSES = set(env_list("ALERT_STATUSES", "Out"))
MIN_OWNED = float(os.environ.get("MIN_OWNED", "20"))
NEWS_UPDATES = os.environ.get("NEWS_UPDATES", "true").lower() == "true"
ALERT_ON_RETURN = os.environ.get("ALERT_ON_RETURN", "true").lower() == "true"
POLL_MINUTES = float(os.environ.get("POLL_MINUTES", "10"))
SUGGESTIONS = int(os.environ.get("SUGGESTIONS", "3"))
MY_PLAYERS = {n.lower() for n in env_list("MY_PLAYERS")}

LOCAL_TZ = ZoneInfo("Europe/Istanbul")
US_TZ = ZoneInfo("America/New_York")
# Fill-in ranking, in minutes per game: one spot further down the depth chart costs 6,
# and a player who is probably on waivers gains 5, since a rostered one can't be picked up.
DEPTH_PENALTY = 6
AVAILABLE_BONUS = 5
AVAILABLE_BELOW = 50  # % owned
DIGEST_LEAD_HOURS = float(os.environ.get("DIGEST_LEAD_HOURS", "6"))  # 0 turns the digest off
DIGEST_PICKS = 2  # fill-ins shown per Out player


# ── Telegram ─────────────────────────────────────────────────────────────────
def tg(method, **params):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    res = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=params, timeout=30)
    res.raise_for_status()
    return res.json()["result"]


def send(text):
    tg("sendMessage", chat_id=os.environ["TELEGRAM_CHAT_ID"], text=text[:4096],
       parse_mode="HTML", disable_web_page_preview=True)


# ── Fill-in logic ────────────────────────────────────────────────────────────
def fill_ins(injured):
    """Healthy players listed behind the injured player at each of his depth chart spots."""
    chart = espn.depth_chart(injured["team_id"])
    best = {}  # id -> {"name", "depth", "positions"}
    for pos, players in chart.items():
        ids = [p["id"] for p in players]
        if injured["id"] not in ids:
            continue
        behind = [p for p in players[ids.index(injured["id"]) + 1:] if not p["out"]]
        for depth, p in enumerate(behind):
            c = best.setdefault(p["id"], {"id": p["id"], "name": p["name"], "depth": depth, "positions": []})
            c["depth"] = min(c["depth"], depth)
            c["positions"].append(pos.upper())
    for c in best.values():
        c.update(espn.player_overview(c["id"]))
        c["recent"] = espn.recent_minutes(c["id"])
    return sorted(best.values(), key=lambda c: (-pickup_score(c), c["depth"], -len(c["positions"])))[:SUGGESTIONS]


def pickup_score(c):
    """Recent minutes, minus a penalty per depth spot, plus a bonus if he's probably on waivers."""
    minutes = c["recent"]["min"] if c.get("recent") else c.get("min") or 0
    available = c.get("owned") is not None and c["owned"] < AVAILABLE_BELOW
    return minutes - DEPTH_PENALTY * c["depth"] + (AVAILABLE_BONUS if available else 0)


def game_line(team_id):
    """'Tue 3 Nov 03:30 vs ATL (in 5h) · 2 left this week', or '' if the schedule is unavailable."""
    games = espn.upcoming_games(team_id)
    if not games:
        return ""
    now = datetime.now(timezone.utc)
    next_game = games[0]
    hours = (next_game["date"] - now).total_seconds() / 3600
    when = "in under 1h" if hours < 1 else f"in {hours:.0f}h" if hours < 48 else f"in {hours / 24:.0f}d"
    local = next_game["date"].astimezone(LOCAL_TZ)
    # Fantasy weeks run Monday to Sunday on US dates, so a 03:30 Monday TR game still counts for Sunday.
    today = now.astimezone(US_TZ).date()
    sunday = today + timedelta(days=6 - today.weekday())
    left = sum(g["date"].astimezone(US_TZ).date() <= sunday for g in games)
    return (f"{local:%a} {local.day} {local:%b %H:%M} {'vs' if next_game['home'] else '@'} {next_game['opponent']} "
            f"({when}) · {left} left this week")


def fmt_stats(p):
    parts = []
    if p.get("recent"):
        parts.append(f"L{p['recent']['games']} {p['recent']['min']:.1f} min")
    parts += [f"{p[k]:.1f} {label}" for k, label in
              [("min", "min"), ("pts", "pts"), ("reb", "reb"), ("ast", "ast")] if p.get(k) is not None]
    if parts and p.get("gp") is not None:
        parts[-1] += f" ({p['gp']:.0f} GP)"
    return " · ".join(parts)


def fmt_owned(owned):
    if owned is None:
        return ""
    return f"{owned:.0f}% owned" + (" 🟢 likely available" if owned < 50 else "")


def esc(s):
    return html.escape(s, quote=False)  # Telegram HTML only needs <, > and & escaped


def is_mine(p):
    return p["name"].lower() in MY_PLAYERS


def injury_message(p, suggestions, next_game=""):
    e = esc
    lines = [f"🚑 <b>{e(p['name'])}</b> ({e(p['team'])} · {e(p['position'])}) is <b>{e(p['status'])}</b>"]
    injury = p["injury"] + (f" · back ~{p['return_date']}" if p["return_date"] else "")
    if injury:
        lines.append(f"🩹 {e(injury)}")
    if next_game:
        lines.append(f"🕒 Next game: {e(next_game)}")
    if is_mine(p):
        lines.append("⚠️ <b>Your player</b>: move him to IL.")
    if p["short"]:
        lines.append(f"\n📰 {e(p['short'])}")
    if p["long"]:
        long = p["long"] if len(p["long"]) < 700 else p["long"][:700].rsplit(" ", 1)[0] + "…"
        lines.append(f"<i>{e(long)}</i>")
    lines.append("")
    if suggestions:
        lines.append("🔁 <b>Who steps up (depth chart):</b>")
        for i, s in enumerate(suggestions, 1):
            spot = "next" if s["depth"] == 0 else f"{s['depth'] + 1} spots behind"
            lines.append(f"{i}. <b>{e(s['name'])}</b>: {spot} at {', '.join(s['positions'])}")
            details = " · ".join(filter(None, [fmt_stats(s), fmt_owned(s["owned"])]))
            if details:
                lines.append(f"    {e(details)}")
        lines.append("\nCheck they're free agents in your Yahoo league before adding.")
    else:
        lines.append("No depth chart fill-ins found for him.")
    return "\n".join(lines)


def full_alert(p):
    return injury_message(p, fill_ins(p), game_line(p["team_id"]))


def update_message(p):
    e = esc
    return f"📰 <b>{e(p['name'])}</b> ({e(p['team'])}, {e(p['status'])}) update:\n{e(p['short'])}"


def return_message(name, team, new_status):
    e = esc
    now = f"now listed <b>{e(new_status)}</b>" if new_status else "off the injury report"
    return f"✅ <b>{e(name)}</b> ({e(team)}) is no longer Out, {now}."


# ── Main loop ────────────────────────────────────────────────────────────────
def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def relevant(entry, aid, name):
    """Fantasy-relevant = on your roster or >= MIN_OWNED% owned on ESPN. Cached in state."""
    if entry.get("relevant") is None:
        owned = espn.player_overview(aid)["owned"]
        entry["relevant"] = name.lower() in MY_PLAYERS or (owned or 0) >= MIN_OWNED
    return entry["relevant"]


def check():
    current = espn.injuries()
    first_run = not STATE_FILE.exists()
    prev = {} if first_run else json.loads(STATE_FILE.read_text())
    state = {}
    failed = []

    for aid, p in current.items():
        old = prev.get(aid, {})
        entry = {"name": p["name"], "team": p["team"], "status": p["status"], "short": p["short"],
                 "relevant": old.get("relevant")}
        state[aid] = entry
        if first_run or p["status"] not in ALERT_STATUSES:
            continue
        try:
            if old.get("status") not in ALERT_STATUSES:
                if relevant(entry, aid, p["name"]):
                    log(f"OUT: {p['name']} ({p['team']})")
                    send(full_alert(p))
            elif NEWS_UPDATES and p["short"] and p["short"] != old.get("short") and relevant(entry, aid, p["name"]):
                log(f"UPDATE: {p['name']}")
                send(update_message(p))
        except Exception:
            # Keep the previous state so the next run sees the same change and retries this alert.
            log(f"Alert failed for {p['name']}:\n" + traceback.format_exc())
            failed.append(p["name"])
            if old:
                state[aid] = old
            else:
                del state[aid]

    if ALERT_ON_RETURN and not first_run:
        for aid, old in prev.items():
            new_status = current[aid]["status"] if aid in current else ""
            if old["status"] not in ALERT_STATUSES or new_status in ALERT_STATUSES:
                continue
            try:
                if relevant(old, aid, old["name"]):
                    log(f"BACK: {old['name']}")
                    send(return_message(old["name"], old["team"], new_status))
            except Exception:
                log(f"Return alert failed for {old['name']}:\n" + traceback.format_exc())
                failed.append(old["name"])
                state[aid] = old

    if first_run:
        log(f"First run: recorded {len(current)} players on the injury report. Alerts start from the next change.")
    else:
        log(f"Checked {len(current)} injury report entries.")
    STATE_FILE.write_text(json.dumps(state, indent=2))
    if failed:
        raise RuntimeError(f"Alerts failed, will retry next run: {', '.join(failed)}")


# ── Daily digest ─────────────────────────────────────────────────────────────
def digest_blocks(games):
    """One text block per game that has fantasy-relevant Out or Day-To-Day players."""
    e = esc
    current = espn.injuries()
    blocks = []
    for g in games:
        ids = {g["home"]["id"], g["away"]["id"]}
        hurt = []
        for p in current.values():
            if p["team_id"] in ids and p["status"] in ("Out", "Day-To-Day"):
                owned = espn.player_overview(p["id"])["owned"]
                if is_mine(p) or (owned or 0) >= MIN_OWNED:
                    hurt.append((p, owned or 0))
        if not hurt:
            continue
        hurt.sort(key=lambda h: (h[0]["status"] != "Out", -h[1]))
        lines = [f"<b>{e(g['away']['abbr'])} @ {e(g['home']['abbr'])}</b> · {g['date'].astimezone(LOCAL_TZ):%H:%M}"]
        for p, owned in hurt:
            icon = "🚑" if p["status"] == "Out" else "🤕"
            lines.append(f"{icon} <b>{e(p['name'])}</b> ({e(p['team'])}) {e(p['status'])} · {owned:.0f}% owned")
            if p["status"] == "Out":
                addable = [s for s in fill_ins(p) if (s.get("owned") or 0) < AVAILABLE_BELOW]
                picks = [digest_pick(s, current) for s in addable[:DIGEST_PICKS]]
                if picks:
                    lines.append("    ➡️ " + " · ".join(picks))
        blocks.append("\n".join(lines))
    return blocks


def digest_pick(s, current):
    """'Obi Toppin ⚠️ Day-To-Day (L5 23.0 min, 12%)': a fill-in who may not play himself gets flagged."""
    listed = current.get(s["id"], {}).get("status")
    details = [f"L{s['recent']['games']} {s['recent']['min']:.1f} min"] if s.get("recent") else []
    if s.get("owned") is not None:
        details.append(f"{s['owned']:.0f}%")
    return esc(s["name"] + (f" ⚠️ {listed}" if listed else "") + (f" ({', '.join(details)})" if details else ""))


def digest_messages(games):
    """Header plus game blocks, split between games so no Telegram message passes its 4096 limit."""
    blocks = digest_blocks(games) or ["No fantasy-relevant injuries in these games."]
    messages = [f"📋 <b>Tonight's injuries</b> ({len(games)} games, Istanbul time)"]
    for block in blocks:
        if len(messages[-1]) + len(block) + 2 > 4000:
            messages.append(block)
        else:
            messages[-1] += "\n\n" + block
    return messages


def digest_games():
    """Today's games that haven't started yet, on the US date the NBA schedules them by."""
    day = datetime.now(US_TZ).strftime("%Y%m%d")
    return day, [g for g in espn.games_on(day) if g["state"] == "pre"]


def send_digest_if_due():
    """Once per US date, DIGEST_LEAD_HOURS before the first game still to start."""
    if DIGEST_LEAD_HOURS <= 0:
        return
    day, games = digest_games()
    last = json.loads(DIGEST_FILE.read_text())["last_sent"] if DIGEST_FILE.exists() else ""
    if day == last or not games:
        return
    if datetime.now(timezone.utc) < games[0]["date"] - timedelta(hours=DIGEST_LEAD_HOURS):
        return
    log(f"Sending digest for {day} ({len(games)} games).")
    for message in digest_messages(games):
        send(message)
    DIGEST_FILE.write_text(json.dumps({"last_sent": day}) + "\n")


def tick():
    """One full round. A failed check still gets its digest, and the error still surfaces afterwards."""
    try:
        check()
    finally:
        send_digest_if_due()


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"

    if cmd == "chatid":
        updates = tg("getUpdates")
        chats = {u["message"]["chat"]["id"]: u["message"]["chat"].get("first_name", "")
                 for u in updates if "message" in u}
        if not chats:
            print("No messages found. Send any message to your bot in Telegram, then rerun.")
        for cid, name in chats.items():
            print(f"TELEGRAM_CHAT_ID={cid}   ({name})")
        return

    if cmd == "once":
        tick()
        return

    if cmd == "digest":
        _, games = digest_games()
        for message in digest_messages(games) if games else ["No games left to start today."]:
            print(message + "\n")
        return

    if cmd == "test":
        current = espn.injuries()
        query = " ".join(sys.argv[2:]).lower()
        if query:
            matches = [p for p in current.values() if query in p["name"].lower()]
        else:  # the most-owned player currently Out
            outs = [p for p in current.values() if p["status"] in ALERT_STATUSES]
            matches = sorted(outs, key=lambda p: -(espn.player_overview(p["id"])["owned"] or 0))
        if not matches:
            print(f"No player matching '{query}' on the injury report.")
            return
        p = matches[0]
        send("🧪 TEST ALERT\n\n" + full_alert(p))
        print(f"Sent test alert for {p['name']}.")
        return

    log(f"Watching ESPN injury report for {sorted(ALERT_STATUSES)} every {POLL_MINUTES:g} min "
        f"(players >= {MIN_OWNED:g}% owned).")
    while True:
        try:
            tick()
        except Exception:
            log("Check failed:\n" + traceback.format_exc())
        time.sleep(POLL_MINUTES * 60)


if __name__ == "__main__":
    main()
