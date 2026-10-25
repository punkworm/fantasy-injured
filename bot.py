"""NBA injury alert bot -> Telegram, with depth-chart fill-in suggestions (ESPN data).

Usage:
  python bot.py chatid          find your Telegram chat id (message the bot first)
  python bot.py test [name]     send a sample alert for a player on the injury report
  python bot.py once            run a single check (for cron / GitHub Actions)
  python bot.py                 run forever, polling every POLL_MINUTES
"""
import html
import json
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

import espn

load_dotenv(Path(__file__).with_name(".env"))
sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to a non-UTF-8 codepage

STATE_FILE = Path(__file__).with_name("state.json")


def env_list(name, default=""):
    return [s.strip() for s in os.environ.get(name, default).split(",") if s.strip()]


ALERT_STATUSES = set(env_list("ALERT_STATUSES", "Out"))
MIN_OWNED = float(os.environ.get("MIN_OWNED", "20"))
NEWS_UPDATES = os.environ.get("NEWS_UPDATES", "true").lower() == "true"
ALERT_ON_RETURN = os.environ.get("ALERT_ON_RETURN", "true").lower() == "true"
POLL_MINUTES = float(os.environ.get("POLL_MINUTES", "10"))
SUGGESTIONS = int(os.environ.get("SUGGESTIONS", "3"))
MY_PLAYERS = {n.lower() for n in env_list("MY_PLAYERS")}


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
    ranked = sorted(best.values(), key=lambda c: (c["depth"], -len(c["positions"])))[:SUGGESTIONS]
    for c in ranked:
        c.update(espn.player_overview(c["id"]))
    return ranked


def fmt_stats(p):
    parts = [f"{p[k]:.1f} {label}" for k, label in
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


def injury_message(p, suggestions):
    e = esc
    lines = [f"🚑 <b>{e(p['name'])}</b> ({e(p['team'])} · {e(p['position'])}) is <b>{e(p['status'])}</b>"]
    injury = p["injury"] + (f" · back ~{p['return_date']}" if p["return_date"] else "")
    if injury:
        lines.append(f"🩹 {e(injury)}")
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

    for aid, p in current.items():
        old = prev.get(aid, {})
        entry = {"name": p["name"], "team": p["team"], "status": p["status"], "short": p["short"],
                 "relevant": old.get("relevant")}
        state[aid] = entry
        if first_run or p["status"] not in ALERT_STATUSES:
            continue
        if old.get("status") not in ALERT_STATUSES:
            if relevant(entry, aid, p["name"]):
                log(f"OUT: {p['name']} ({p['team']})")
                send(injury_message(p, fill_ins(p)))
        elif NEWS_UPDATES and p["short"] and p["short"] != old.get("short") and relevant(entry, aid, p["name"]):
            log(f"UPDATE: {p['name']}")
            send(update_message(p))

    if ALERT_ON_RETURN and not first_run:
        for aid, old in prev.items():
            new_status = current[aid]["status"] if aid in current else ""
            if old["status"] in ALERT_STATUSES and new_status not in ALERT_STATUSES \
                    and relevant(old, aid, old["name"]):
                log(f"BACK: {old['name']}")
                send(return_message(old["name"], old["team"], new_status))

    if first_run:
        log(f"First run: recorded {len(current)} players on the injury report. Alerts start from the next change.")
    else:
        log(f"Checked {len(current)} injury report entries.")
    STATE_FILE.write_text(json.dumps(state, indent=2))


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
        check()
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
        send("🧪 TEST ALERT\n\n" + injury_message(p, fill_ins(p)))
        print(f"Sent test alert for {p['name']}.")
        return

    log(f"Watching ESPN injury report for {sorted(ALERT_STATUSES)} every {POLL_MINUTES:g} min "
        f"(players >= {MIN_OWNED:g}% owned).")
    while True:
        try:
            check()
        except Exception:
            log("Check failed:\n" + traceback.format_exc())
        time.sleep(POLL_MINUTES * 60)


if __name__ == "__main__":
    main()
