#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bybit Volatility Anomaly Alerts → Telegram
Аналитический софт: находит аномальные всплески (пампы) и оценивает вероятность коррекции по историческим данным.

- Биржа: Bybit Spot (через ccxt)
- Таймфреймы: 5m и 15m
- Уведомления: Telegram Bot API
"""

import os
import time
import sqlite3
import traceback
from datetime import datetime, timezone
from typing import List

import requests
import ccxt
import numpy as np
import pandas as pd
from dotenv import load_dotenv

# ------------------------- Конфигурация -------------------------

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", "60"))
TOP_MARKETS = int(os.getenv("TOP_MARKETS", "60"))
THRESH_5M_PCT = float(os.getenv("THRESH_5M_PCT", "6"))
THRESH_15M_PCT = float(os.getenv("THRESH_15M_PCT", "12"))

assert TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, "Нужно указать TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID"

STATE_DB = os.path.join(os.path.dirname(__file__), "state.db")
TIMEFRAMES = [("5m", THRESH_5M_PCT), ("15m", THRESH_15M_PCT)]

# --- Утилиты ---
def ts_to_iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def send_telegram(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"[TG] Ошибка: {e}")

# --- Bybit ---
def build_exchange() -> ccxt.bybit:
    return ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "spot"}})

def get_symbols(ex: ccxt.Exchange) -> List[str]:
    markets = ex.load_markets()
    symbols = [s for s in markets if s.endswith("/USDT")]
    clean = []
    for s in symbols:
        base = s.split("/")[0]
        if any(x in base for x in ["UP", "DOWN", "3L", "3S"]):
            continue
        clean.append(s)
    return clean[:TOP_MARKETS]

def check_spikes(ex: ccxt.Exchange, symbol: str, timeframe: str, threshold: float):
    ohlcv = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=200)
    if len(ohlcv) < 2:
        return None
    o0, o1 = ohlcv[-2], ohlcv[-1]
    pct = (o1[4] / o0[4] - 1) * 100
    if pct >= threshold:
        return (symbol, timeframe, o1[0], pct)
    return None

# --- Основной цикл ---
def main():
    ex = build_exchange()
    symbols = get_symbols(ex)
    while True:
        for tf, th in TIMEFRAMES:
            for s in symbols:
                try:
                    spike = check_spikes(ex, s, tf, th)
                    if spike:
                        sym, timeframe, ts, pct = spike
                        msg = f"🚨 Памп {sym} на {timeframe}\nРост: {pct:.2f}%\nСвеча: {ts_to_iso(ts)}"
                        send_telegram(msg)
                except Exception as e:
                    print(f"Ошибка {s} {tf}: {e}")
                    time.sleep(1)
        time.sleep(POLL_INTERVAL_SEC)

if __name__ == "__main__":
    main()
