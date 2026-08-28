#!/usr/bin/env python3
"""
Pulls price data for tracked indices, gold, defense/war-tech stocks, and the
user's personal holdings, and prints a structured JSON snapshot to stdout.

This script only computes numbers - it does not fetch news or write any
narrative. That's deliberately left to the calling agent, which has web
search available and can synthesize an honest, sourced summary instead of
this script guessing at "why" a move happened.
"""
import json
import sys
from datetime import date

import yfinance as yf

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

# A weekly move at or below this on a broad index counts as "market bleeding".
WEEKLY_DIP_THRESHOLD_PCT = -5.0

ALL_TICKERS = {**INDICES, **GOLD, **DEFENSE_WAR_TECH, **HOLDINGS}


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


def main():
    snapshot = fetch_snapshot()

    dip_triggers = []
    for ticker in INDICES:
        entry = snapshot.get(ticker, {})
        wc = entry.get("week_change_pct")
        if wc is not None and wc <= WEEKLY_DIP_THRESHOLD_PCT:
            dip_triggers.append({"ticker": ticker, "label": entry["label"], "week_change_pct": wc})

    result = {
        "date": date.today().isoformat(),
        "dip_alert": len(dip_triggers) > 0,
        "dip_triggers": dip_triggers,
        "weekly_dip_threshold_pct": WEEKLY_DIP_THRESHOLD_PCT,
        "snapshot": {
            ticker: {**entry, "category": classify(ticker)}
            for ticker, entry in snapshot.items()
        },
    }
    json.dump(result, sys.stdout, indent=2)


if __name__ == "__main__":
    main()
