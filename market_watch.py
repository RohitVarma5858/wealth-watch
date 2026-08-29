#!/usr/bin/env python3
"""
Uses the Twelve Data API (not yfinance/Yahoo Finance - Yahoo's undocumented
API gets blocked by the cloud sandbox's egress policy, likely because
scraping it violates Yahoo's ToS; Twelve Data is a proper registered API).

Coverage note: Twelve Data's free plan only covers US-listed securities.
The user's Canadian-listed ETFs (VFV.TO, XQQ.TO, XIC.TO, TEC.TO) and raw
index symbols (SPX, NDX) are not available on the free plan, so broad
"index" exposure is approximated with SPY (S&P 500) and QQQ (Nasdaq 100)
instead. VIX is dropped entirely - there's no ETF that tracks it accurately
enough to be worth showing.

The free plan is also rate-limited to 8 credits/minute (each symbol in a
request = 1 credit), so any multi-symbol call is chunked into groups of 7
with a cooldown between chunks.

Two modes:

  check  - lightweight, self-contained hourly run. Checks SPY/QQQ for an
           intraday crash (>=5% below yesterday's close) or a sustained
           weekly decline (>=5% over the trailing week), and sends an ntfy
           alert directly if triggered, with a cooldown so a sustained drop
           doesn't re-alert every hour. No news research - never calls out
           to an LLM.

  digest - fetches the full watchlist snapshot and prints it as JSON. Does
           NOT send any notification or write any report - that's left to
           the calling agent, which has web search available to research
           *why* things moved instead of this script guessing.
"""
import json
import os
import sys
import time
from datetime import date

import requests

API_KEY = os.environ["TWELVEDATA_API_KEY"]
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "wealth_watch_alerts")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
BASE_URL = "https://api.twelvedata.com"

CHUNK_SIZE = 7
CHUNK_COOLDOWN_SECONDS = 65

INDICES = {  # ETF proxies - see module docstring
    "SPY": "S&P 500 (SPY proxy)",
    "QQQ": "Nasdaq 100 (QQQ proxy)",
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
    "MRVL": "Marvell Technology",
    "NVDA": "Nvidia",
    "CHPS": "Semiconductor ETF (CHPS)",
    "MSFT": "Microsoft",
    "AMZN": "Amazon",
}

ALL_TICKERS = {**INDICES, **GOLD, **DEFENSE_WAR_TECH, **HOLDINGS}

DIP_THRESHOLD_PCT = -5.0
REALERT_DEEPENING_PCT = 2.0  # only re-alert same day if the drop gets at least this much worse


def chunked(items, size):
    items = list(items)
    for i in range(0, len(items), size):
        yield items[i:i + size]


MAX_429_RETRIES = 5


def td_get(endpoint, symbols, extra_params=None):
    """Chunked GET across `symbols`, merging results into one dict keyed by symbol.

    Retries on 429 rather than failing outright: back-to-back runs (or an
    hourly check overlapping a digest run) can leave the per-minute credit
    window already partly consumed when a chunk fires.
    """
    merged = {}
    for i, chunk in enumerate(chunked(symbols, CHUNK_SIZE)):
        if i > 0:
            time.sleep(CHUNK_COOLDOWN_SECONDS)
        params = {"symbol": ",".join(chunk), "apikey": API_KEY, **(extra_params or {})}
        for attempt in range(MAX_429_RETRIES + 1):
            r = requests.get(f"{BASE_URL}/{endpoint}", params=params, timeout=30)
            if r.status_code != 429:
                break
            if attempt == MAX_429_RETRIES:
                r.raise_for_status()
            time.sleep(CHUNK_COOLDOWN_SECONDS)
        r.raise_for_status()
        data = r.json()
        if len(chunk) == 1:
            merged[chunk[0]] = data
        else:
            merged.update(data)
    return merged


def fetch_quotes(symbols):
    raw = td_get("quote", symbols)
    result = {}
    for sym in symbols:
        entry = raw.get(sym, {})
        if "close" not in entry or "percent_change" not in entry:
            result[sym] = {"error": entry.get("message", "no data")}
            continue
        result[sym] = {
            "price": round(float(entry["close"]), 2),
            "day_change_pct": round(float(entry["percent_change"]), 2),
        }
    return result


def fetch_week_changes(symbols):
    raw = td_get("time_series", symbols, {"interval": "1day", "outputsize": "6"})
    result = {}
    for sym in symbols:
        entry = raw.get(sym, {})
        values = entry.get("values")
        if not values or len(values) < 2:
            result[sym] = None
            continue
        latest_close = float(values[0]["close"])
        oldest_close = float(values[-1]["close"])
        result[sym] = round((latest_close / oldest_close - 1) * 100, 2)
    return result


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


def run_check():
    state = load_state()
    index_symbols = list(INDICES.keys())

    quotes = fetch_quotes(index_symbols)
    worst = None  # (pct, ticker, label, trigger_type)

    for sym, label in INDICES.items():
        dc = quotes.get(sym, {}).get("day_change_pct")
        if dc is not None and dc <= DIP_THRESHOLD_PCT:
            if worst is None or dc < worst[0]:
                worst = (dc, sym, label, "intraday")

    if worst is None:
        week_changes = fetch_week_changes(index_symbols)
        for sym, label in INDICES.items():
            wc = week_changes.get(sym)
            if wc is not None and wc <= DIP_THRESHOLD_PCT:
                if worst is None or wc < worst[0]:
                    worst = (wc, sym, label, "weekly")

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
    symbols = list(ALL_TICKERS.keys())
    quotes = fetch_quotes(symbols)
    week_changes = fetch_week_changes(symbols)

    snapshot = {}
    for sym, label in ALL_TICKERS.items():
        q = quotes.get(sym, {})
        if "error" in q:
            snapshot[sym] = {"label": label, "category": classify(sym), "error": q["error"]}
            continue
        snapshot[sym] = {
            "label": label,
            "category": classify(sym),
            "last_close": q.get("price"),
            "day_change_pct": q.get("day_change_pct"),
            "week_change_pct": week_changes.get(sym),
        }

    result = {"date": date.today().isoformat(), "snapshot": snapshot}
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
