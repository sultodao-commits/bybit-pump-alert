#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bybit Volatility Anomaly Alerts → Telegram (Scalingo)
- Источник данных: Bybit Spot через ccxt
- Мониторинг таймфреймов: 5m и 15m
- Алёрты: Telegram Bot API
- Де-дубль: sqlite (по паре/таймфрейму/времени свечи)
"""

import os
import time
import sqlite3
import traceback
from datetime import datetime, timezone
from typing import List, Dict, Tuple

import requests
import ccxt
import numpy as np
import pandas as pd
from dotenv import load_dotenv

# ------------------------- Конфигурация -------------------------

# На Scalingo переменные приходят из окружения; dotenv не обязателен, но не мешает.
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Настраиваемые параметры (есть дефолты)
POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", "60"))   # период цикла мониторинга
TOP_MARKETS       = int(os.getenv("TOP_MARKETS", "60"))         # сколько топ USDT-пар мониторить
THRESH_5M_PCT     = float(os.getenv("THRESH_5M_PCT", "6"))      # порог пампа за 1 свечу 5m
THRESH_15M_PCT    = float(os.getenv("THRESH_15M_PCT", "12"))    # порог пампа за 1 свечу 15m

# Проверка обязательных переменных
assert TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, "Нужно указать TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID"

# Внутренние параметры
STATE_DB   = os.path.join(os.path.dirname(__file__), "state.db")
TIMEFRAMES = [("5m", THRESH_5M_PCT), ("15m", THRESH_15M_PCT)]


# ------------------------- Утилиты -------------------------

def ts_to_iso(ts_ms: int) -> str:
    """Милисекунды → ISO в UTC"""
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def send_telegram(text: str) -> None:
    """Отправка сообщения в Telegram"""
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
            print(f"[TG] Ошибка отправки: {r.status_code} {r.text}")
    except Exception as e:
        print(f"[TG] Исключение при отправке: {e}")

def init_db() -> None:
    """Создаём таблицу для де-дублирования сигналов"""
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
    cur.execute(
        "SELECT 1 FROM alerts WHERE symbol=? AND timeframe=? AND candle_ts=?",
        (symbol, timeframe, candle_ts),
    )
    row = cur.fetchone()
    con.close()
    return row is not None

def save_alert(symbol: str, timeframe: str, candle_ts: int) -> None:
    con = sqlite3.connect(STATE_DB)
    cur = con.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO alerts(symbol, timeframe, candle_ts) VALUES(?,?,?)",
        (symbol, timeframe, candle_ts),
    )
    con.commit()
    con.close()


# ------------------------- Работа с Bybit через ccxt -------------------------

def build_exchange() -> ccxt.bybit:
    """Инициализация Bybit (спот)"""
    ex = ccxt.bybit({
        "enableRateLimit": True,
        "timeout": 20000,
        "options": {"defaultType": "spot"},
    })
    return ex

def pick_spot_usdt_symbols(ex: ccxt.Exchange, top_n: int) -> List[str]:
    """
    Берём активные SPOT пары c котировкой USDT, исключаем 3L/3S/UP/DOWN и стейблы как base.
    Сортируем по 24h объёму (quoteVolume/baseVolume) и берём top_n.
    """
    markets = ex.load_markets(reload=True)
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
            candidates.append(sym)
        except Exception:
            continue

    tickers = ex.fetch_tickers(params={"type": "spot"})
    rows = []
    for sym in candidates:
        t = tickers.get(sym, {})
        qv = t.get("quoteVolume") or t.get("baseVolume") or 0.0
        rows.append((sym, float(qv)))
    rows.sort(key=lambda x: x[1], reverse=True)
    return [sym for sym, _ in rows[:max(10, top_n)]]


# ------------------------- Аналитика свечи -------------------------

def last_bar_change_pct(ohlcv: list) -> Tuple[float, int]:
    """
    Возвращает (% изменения последней свечи к предыдущей, timestamp последней свечи)
    ohlcv: список [ts, open, high, low, close, volume]
    """
    if not ohlcv or len(ohlcv) < 2:
        return 0.0, 0
    prev = ohlcv[-2][4]
    last = ohlcv[-1][4]
    ts   = int(ohlcv[-1][0])
    if prev == 0:
        return 0.0, ts
    return (last / prev - 1.0) * 100.0, ts


# ------------------------- Основной цикл -------------------------

def main():
    print("Инициализация...")
    init_db()

    # Пингуем в Telegram при старте
    try:
        send_telegram("✅ Бот запущен: мониторинг пампов на Bybit (5m/15m).")
    except Exception as e:
        print(f"[BOOT PING] Ошибка отправки: {e}")

    ex = build_exchange()

    # Подбираем список пар
    try:
        symbols = pick_spot_usdt_symbols(ex, TOP_MARKETS)
    except Exception as e:
        symbols = []
        print(f"[SYMBOLS] Ошибка подбора пар: {e}")
        traceback.print_exc()

    print(f"Всего пар к мониторингу: {len(symbols)}")
    if symbols[:10]:
        print(f"Первые 10: {symbols[:10]}")

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
                                f"🚨 <b>Памп обнаружен</b> ({timeframe})\n"
                                f"Монета: <b>{symbol}</b>\n"
                                f"Рост последней свечи: <b>{chg_pct:.2f}%</b>\n"
                                f"Свеча: {ts_to_iso(candle_ts)}\n\n"
                                f"<i>Не финсовет. Проверьте ликвидность и риски.</i>"
                            )
                            send_telegram(msg)
                            save_alert(symbol, timeframe, candle_ts)
                            # маленькая пауза, щадим API
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

        # Поддерживаем период опроса
        elapsed = time.time() - cycle_start
        sleep_left = max(1.0, POLL_INTERVAL_SEC - elapsed)
        time.sleep(sleep_left)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Остановка по Ctrl+C")
