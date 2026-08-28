#!/usr/bin/env python3
"""
Two modes:

  check  - lightweight, self-contained hourly run. Fetches near-live prices
           for the broad indices, checks for an intraday crash (>=5% below
           yesterday's close) or a sustained weekly decline (>=5% over the
           trailing week), and sends an ntfy alert directly if triggered
           (with a cooldown so a sustained drop doesn't re-alert every hour).
           No news research - this mode never calls out to an LLM.

  digest - fetches the full snapshot (indices, gold, defense/war-tech,
           personal holdings) and prints it as JSON. Does NOT send any
           notification or write any report - that's left to the calling
           agent, which has web search available to research *why* things
           moved and can write an honest, sourced weekly summary instead of
           this script guessing.
"""
import json
import os
import sys
from datetime import date

import requests
import yfinance as yf

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "wealth_watch_alerts")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

INDICES = {
    "^GSPC": "S&P 500",
    "^NDX": "Nasdaq 100",
    "^GSPTSE": "TSX Composite",
    "^VIX": "VIX (volatility)",
}

GOLD = {
    "GLD": "Gold ETF (GLD)",
}

DEFENSE_WAR_TECH = {
    "LMT": "Lockheed Martin",
    "RTX": "RTX Corp",
    "NOC": "Northrop Grumman",
    "GD": "General Dynamics",
    "PLTR": "Palantir",
}

HOLDINGS = {
    "VFV.TO": "Vanguard S&P 500 (CAD)",
    "XQQ.TO": "iShares Nasdaq 100 (CAD)",
    "XIC.TO": "iShares Core S&P/TSX Composite",
    "TEC.TO": "Tech ETF (TEC.TO)",
    "MRVL": "Marvell Technology",
    "NVDA": "Nvidia",
    "CHPS": "Semiconductor ETF (CHPS)",
    "MSFT": "Microsoft",
    "AMZN": "Amazon",
}

ALL_TICKERS = {**INDICES, **GOLD, **DEFENSE_WAR_TECH, **HOLDINGS}

# VIX is excluded from dip-alert triggers: VIX *falling* means fear is
# decreasing (a calm signal, not a crash), so the "down = bad" dip logic
# below doesn't apply to it. It's still shown in the snapshot/digest.
DIP_CHECK_INDICES = {k: v for k, v in INDICES.items() if k != "^VIX"}

DIP_THRESHOLD_PCT = -5.0
REALERT_DEEPENING_PCT = 2.0  # only re-alert same day if the drop gets at least this much worse


def pct_change(series):
    if len(series) < 2:
        return None
    return round((series.iloc[-1] / series.iloc[0] - 1) * 100, 2)


def fetch_snapshot():
    data = yf.download(list(ALL_TICKERS.keys()), period="10d", progress=False, group_by="ticker")
    snapshot = {}
    for ticker, label in ALL_TICKERS.items():
        try:
            closes = data[ticker]["Close"].dropna()
        except Exception:
            closes = []
        if len(closes) == 0:
            snapshot[ticker] = {"label": label, "error": "no data"}
            continue
        last_close = round(float(closes.iloc[-1]), 2)
        day_change = pct_change(closes.iloc[-2:]) if len(closes) >= 2 else None
        week_change = pct_change(closes)
        snapshot[ticker] = {
            "label": label,
            "last_close": last_close,
            "day_change_pct": day_change,
            "week_change_pct": week_change,
        }
    return snapshot


def classify(ticker):
    if ticker in INDICES:
        return "index"
    if ticker in GOLD:
        return "gold"
    if ticker in DEFENSE_WAR_TECH:
        return "defense_war_tech"
    return "holding"


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def notify(title, message):
    requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={"Title": title, "Priority": "high"},
        timeout=15,
    )


def intraday_change(ticker):
    try:
        fi = yf.Ticker(ticker).fast_info
        last = fi.get("lastPrice") if hasattr(fi, "get") else fi["lastPrice"]
        prev_close = fi.get("previousClose") if hasattr(fi, "get") else fi["previousClose"]
        if not last or not prev_close:
            return None
        return round((last / prev_close - 1) * 100, 2)
    except Exception:
        return None


def run_check():
    state = load_state()
    worst = None  # (pct, ticker, label, trigger_type)

    for ticker, label in DIP_CHECK_INDICES.items():
        ic = intraday_change(ticker)
        if ic is not None and ic <= DIP_THRESHOLD_PCT:
            if worst is None or ic < worst[0]:
                worst = (ic, ticker, label, "intraday")

    if worst is None:
        # only bother with the (slower) weekly-history fetch if no intraday
        # crash already fired, since it's the same threshold check either way
        snapshot = fetch_snapshot()
        for ticker, label in DIP_CHECK_INDICES.items():
            wc = snapshot.get(ticker, {}).get("week_change_pct")
            if wc is not None and wc <= DIP_THRESHOLD_PCT:
                if worst is None or wc < worst[0]:
                    worst = (wc, ticker, label, "weekly")

    result = {"date": date.today().isoformat(), "triggered": worst is not None}

    if worst is None:
        state["last_checked"] = date.today().isoformat()
        save_state(state)
        print(json.dumps({**result, "note": "no dip detected"}, indent=2))
        return

    pct, ticker, label, trigger_type = worst
    last_alert = state.get("last_alert")
    should_alert = (
        last_alert is None
        or last_alert.get("date") != date.today().isoformat()
        or pct <= last_alert.get("worst_pct", 0) - REALERT_DEEPENING_PCT
    )

    result.update({"ticker": ticker, "label": label, "pct": pct, "trigger_type": trigger_type, "alert_sent": should_alert})

    if should_alert:
        kind = "Intraday crash" if trigger_type == "intraday" else "Weekly decline"
        notify(
            "Market Dip Alert",
            f"{kind}: {label} ({ticker}) is {pct}% "
            f"{'today' if trigger_type == 'intraday' else 'this week'}.\n"
            "This is a signal to look closer, not investment advice.",
        )
        state["last_alert"] = {"date": date.today().isoformat(), "worst_pct": pct, "ticker": ticker, "trigger_type": trigger_type}
        save_state(state)

    print(json.dumps(result, indent=2))


def run_digest():
    snapshot = fetch_snapshot()
    result = {
        "date": date.today().isoformat(),
        "snapshot": {
            ticker: {**entry, "category": classify(ticker)}
            for ticker, entry in snapshot.items()
        },
    }
    print(json.dumps(result, indent=2))


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "digest"
    if mode == "check":
        run_check()
    elif mode == "digest":
        run_digest()
    else:
        print(f"Unknown mode: {mode}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
