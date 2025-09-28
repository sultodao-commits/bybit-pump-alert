#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bybit Volatility Anomaly Alerts → Telegram (Scalingo, c фильтром ликвидности)
- Источник: Bybit Spot (ccxt)
- Таймфреймы: 5m и 15m
- Алерты: Telegram Bot API
- Де-дубль: sqlite (symbol, timeframe, candle_ts)
- Улучшение: фильтр по 24h объёму (USDT) и минимальной цене
"""

import os
import time
import sqlite3
import traceback
from datetime import datetime, timezone
from typing import List, Tuple

import requests
import ccxt
import numpy as np
import pandas as pd
from dotenv import load_dotenv

# ------------------------- Конфигурация -------------------------

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID", "").strip()

POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", "60"))
TOP_MARKETS       = int(os.getenv("TOP_MARKETS", "60"))
THRESH_5M_PCT     = float(os.getenv("THRESH_5M_PCT", "6"))
THRESH_15M_PCT    = float(os.getenv("THRESH_15M_PCT", "12"))

# Новые параметры ликвидности
MIN_24H_QUOTE_VOLUME_USDT = float(os.getenv("MIN_24H_QUOTE_VOLUME_USDT", "500000"))
MIN_LAST_PRICE_USDT       = float(os.getenv("MIN_LAST_PRICE_USDT", "0.002"))

assert TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, "Нужно указать TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID"

STATE_DB   = os.path.join(os.path.dirname(__file__), "state.db")
TIMEFRAMES = [("5m", THRESH_5M_PCT), ("15m", THRESH_15M_PCT)]

# ------------------------- Утилиты -------------------------

def ts_to_iso(ts_ms: int) -> str:
    """мс → ISO (UTC)"""
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def send_telegram(text: str) -> None:
    """Отправить сообщение в Telegram"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        r = requests.post(url, json=payload, timeout=12)
        if r.status_code != 200:
            print(f"[TG] HTTP {r.status_code}: {r.text}")
    except Exception as e:
        print(f"[TG] Исключение: {e}")

def init_db() -> None:
    con = sqlite3.connect(STATE_DB)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            candle_ts INTEGER NOT NULL,
            PRIMARY KEY (symbol, timeframe, candle_ts)
        )
    """)
    con.commit()
    con.close()

def was_alerted(symbol: str, timeframe: str, candle_ts: int) -> bool:
    con = sqlite3.connect(STATE_DB)
    cur = con.cursor()
    cur.execute("SELECT 1 FROM alerts WHERE symbol=? AND timeframe=? AND candle_ts=?", (symbol, timeframe, candle_ts))
    row = cur.fetchone()
    con.close()
    return row is not None

def save_alert(symbol: str, timeframe: str, candle_ts: int) -> None:
    con = sqlite3.connect(STATE_DB)
    cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO alerts(symbol, timeframe, candle_ts) VALUES(?,?,?)", (symbol, timeframe, candle_ts))
    con.commit()
    con.close()

# ------------------------- Bybit (ccxt) -------------------------

def build_exchange() -> ccxt.bybit:
    return ccxt.bybit({
        "enableRateLimit": True,
        "timeout": 20000,
        "options": {"defaultType": "spot"},
    })

def pick_spot_usdt_symbols_with_liquidity(ex: ccxt.Exchange, top_n: int,
                                          min_qv_usdt: float,
                                          min_last_price: float) -> List[str]:
    """
    Выбираем SPOT/USDT пары, исключаем UP/DOWN/3L/3S/4L/4S и стейблы как base.
    Фильтруем по 24h quoteVolume >= min_qv_usdt и last >= min_last_price.
    Сортируем по quoteVolume и берём top_n.
    """
    markets = ex.load_markets(reload=True)
    tickers = ex.fetch_tickers(params={"type": "spot"})

    candidates = []
    for sym, m in markets.items():
        try:
            if m.get("type") != "spot" or not m.get("spot"):
                continue
            if m.get("quote") != "USDT":
                continue
            base = m.get("base", "")
            if any(tag in base for tag in ["UP", "DOWN", "3L", "3S", "4L", "4S"]):
                continue
            if base in ["USDT", "USDC", "FDUSD", "DAI"]:
                continue

            t = tickers.get(sym, {})
            qv = float(t.get("quoteVolume") or t.get("baseVolume") or 0.0)
            last = float(t.get("last") or t.get("close") or 0.0)

            if qv < min_qv_usdt:
                continue
            if last < min_last_price:
                continue

            candidates.append((sym, qv))
        except Exception:
            continue

    candidates.sort(key=lambda x: x[1], reverse=True)
    return [sym for sym, _ in candidates[:max(10, top_n)]]

# ------------------------- Аналитика свечи -------------------------

def last_bar_change_pct(ohlcv: list) -> Tuple[float, int]:
    """
    Возврат: (изменение последней свечи к предыдущей, %, ts последней свечи)
    ohlcv: [ts, open, high, low, close, volume]
    """
    if not ohlcv or len(ohlcv) < 2:
        return 0.0, 0
    prev = float(ohlcv[-2][4])
    last = float(ohlcv[-1][4])
    ts   = int(ohlcv[-1][0])
    if prev == 0:
        return 0.0, ts
    return (last / prev - 1.0) * 100.0, ts

# ------------------------- Основной цикл -------------------------

def main():
    print("Инициализация...")
    init_db()

    try:
        send_telegram(
            "✅ Бот запущен (Scalingo).\n"
            f"Фильтры: объём ≥ {int(MIN_24H_QUOTE_VOLUME_USDT):,} USDT, цена ≥ {MIN_LAST_PRICE_USDT} USDT.\n"
            f"Пороги: 5m ≥ {THRESH_5M_PCT}%, 15m ≥ {THRESH_15M_PCT}%.".replace(",", " ")
        )
    except Exception as e:
        print(f"[BOOT PING] Ошибка TG: {e}")

    ex = build_exchange()

    # Подбираем пары с фильтром ликвидности
    symbols = []
    try:
        symbols = pick_spot_usdt_symbols_with_liquidity(
            ex, TOP_MARKETS,
            MIN_24H_QUOTE_VOLUME_USDT,
            MIN_LAST_PRICE_USDT
        )
        print(f"К мониторингу отобрано пар: {len(symbols)}")
        if symbols[:10]:
            print(f"Первые 10: {symbols[:10]}")
        # Пошлём краткий отчёт о количестве пар
        try:
            send_telegram(f"📊 К мониторингу отобрано пар: <b>{len(symbols)}</b> (по фильтру ликвидности).")
        except Exception as e:
            print(f"[TG] Ошибка отчёта: {e}")
    except Exception as e:
        print(f"[SYMBOLS] Ошибка подбора пар: {e}")
        traceback.print_exc()

    while True:
        cycle_start = time.time()
        try:
            for timeframe, threshold in TIMEFRAMES:
                for symbol in symbols:
                    try:
                        ohlcv = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=200)
                        chg_pct, candle_ts = last_bar_change_pct(ohlcv)
                        if candle_ts == 0:
                            continue
                        if chg_pct >= threshold and not was_alerted(symbol, timeframe, candle_ts):
                            msg = (
                                f"🚨 <b>Памп</b> ({timeframe})\n"
                                f"Монета: <b>{symbol}</b>\n"
                                f"Рост последней свечи: <b>{chg_pct:.2f}%</b>\n"
                                f"Свеча: {ts_to_iso(candle_ts)}\n\n"
                                f"<i>Не финсовет. Проверяйте ликвидность и риски.</i>"
                            )
                            send_telegram(msg)
                            save_alert(symbol, timeframe, candle_ts)
                            time.sleep(0.15)
                    except ccxt.RateLimitExceeded as e:
                        print(f"[{symbol} {timeframe}] Rate limit: {e}. Пауза 3с")
                        time.sleep(3)
                    except Exception as e:
                        print(f"[{symbol} {timeframe}] Ошибка: {e}")
                        traceback.print_exc()
                        time.sleep(0.1)
        except Exception as e:
            print(f"[CYCLE] Ошибка верхнего уровня: {e}")
            traceback.print_exc()

        elapsed = time.time() - cycle_start
        time.sleep(max(1.0, POLL_INTERVAL_SEC - elapsed))

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Остановка по Ctrl+C")
