#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bybit Futures Alerts → Telegram (Pumps + Dumps, History, Revert Time)

— Пампы/Дампы на 5m/15m
— История + пост-эффект (мин/макс, fwd 5/15/30/60м, время до реверта)
— Сообщения только 2 вида: Памп 🚨 и Дамп 🔻
— В каждом сообщении выводится RSI (перегрето/перепроданность/нейтрально)
— Время свечи: UTC и Екатеринбург (UTC+5)
"""

import os
import time
import sqlite3
import traceback
from datetime import datetime, timezone, timedelta
from typing import List, Tuple, Optional, Dict

import requests
import ccxt
from dotenv import load_dotenv

# ------------------------- Конфигурация -------------------------

load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "").strip()
assert TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, "Укажи TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID"

POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", "60"))

THRESH_5M_PCT   = float(os.getenv("THRESH_5M_PCT", "6"))
THRESH_15M_PCT  = float(os.getenv("THRESH_15M_PCT", "12"))
THRESH_5M_DROP_PCT  = float(os.getenv("THRESH_5M_DROP_PCT", "6"))
THRESH_15M_DROP_PCT = float(os.getenv("THRESH_15M_DROP_PCT", "12"))

MIN_24H_QUOTE_VOLUME_USDT = float(os.getenv("MIN_24H_QUOTE_VOLUME_USDT", "500000"))
MIN_LAST_PRICE_USDT       = float(os.getenv("MIN_LAST_PRICE_USDT", "0.002"))

POST_EFFECT_MINUTES = 60
HISTORY_LOOKBACK_DAYS = int(os.getenv("HISTORY_LOOKBACK_DAYS", "30"))

STATE_DB = os.path.join(os.path.dirname(__file__), "state.db")

TIMEFRAMES = [
    ("5m",  THRESH_5M_PCT,  THRESH_5M_DROP_PCT),
    ("15m", THRESH_15M_PCT, THRESH_15M_DROP_PCT),
]

# ------------------------- Утилиты -------------------------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def ts_to_iso(ts_ms: int) -> str:
    dt_utc = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    dt_ekb = dt_utc + timedelta(hours=5)
    return f"{dt_utc.strftime('%Y-%m-%d %H:%M UTC')} | {dt_ekb.strftime('%Y-%m-%d %H:%M ЕКБ')}"

def send_telegram(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code != 200:
            print(f"[TG] HTTP {r.status_code}: {r.text}")
    except Exception as e:
        print(f"[TG] Exception: {e}")

# ------------------------- База -------------------------

def init_db() -> None:
    con = sqlite3.connect(STATE_DB)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS spikes_v2 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key_symbol TEXT NOT NULL,
            timeframe  TEXT NOT NULL,
            direction  TEXT NOT NULL,
            candle_ts  INTEGER NOT NULL,
            price      REAL NOT NULL,
            min_return_60m REAL,
            max_return_60m REAL,
            fwd_5m REAL, fwd_15m REAL, fwd_30m REAL, fwd_60m REAL,
            revert_min INTEGER,
            evaluated INTEGER DEFAULT 0
        )
    """)
    con.commit()
    con.close()

def insert_spike(key_symbol: str, timeframe: str, direction: str, candle_ts: int, price: float) -> None:
    con = sqlite3.connect(STATE_DB); cur = con.cursor()
    cur.execute("""INSERT INTO spikes_v2(key_symbol, timeframe, direction, candle_ts, price)
                   VALUES (?,?,?,?,?)""", (key_symbol, timeframe, direction, int(candle_ts), float(price)))
    con.commit(); con.close()

def recent_symbol_stats(key_symbol: str, timeframe: str, direction: str,
                        days: int = HISTORY_LOOKBACK_DAYS) -> Optional[Dict[str, float]]:
    since_ms = int((now_utc() - timedelta(days=days)).timestamp() * 1000)
    con = sqlite3.connect(STATE_DB); cur = con.cursor()
    cur.execute("""SELECT min_return_60m, max_return_60m, fwd_5m, fwd_15m, fwd_30m, fwd_60m, revert_min
                   FROM spikes_v2 WHERE key_symbol=? AND timeframe=? AND direction=? 
                   AND evaluated=1 AND candle_ts>=?""", (key_symbol, timeframe, direction, since_ms))
    rows = cur.fetchall(); con.close()
    if not rows:
        return None
    def avg_ok(arr): arr = [x for x in arr if x is not None]; return sum(arr)/len(arr) if arr else None
    return {
        "episodes": len(rows),
        "avg_min_60m": avg_ok([r[0] for r in rows]),
        "avg_max_60m": avg_ok([r[1] for r in rows]),
        "avg_fwd_5m":  avg_ok([r[2] for r in rows]),
        "avg_fwd_15m": avg_ok([r[3] for r in rows]),
        "avg_fwd_30m": avg_ok([r[4] for r in rows]),
        "avg_fwd_60m": avg_ok([r[5] for r in rows]),
        "avg_revert_min": avg_ok([r[6] for r in rows]),
    }

# ------------------------- Bybit Futures -------------------------

def ex_swap() -> ccxt.bybit:
    return ccxt.bybit({"enableRateLimit": True, "timeout": 20000, "options": {"defaultType": "swap"}})

def pick_all_swap_usdt_symbols_with_liquidity(ex: ccxt.Exchange,
                                              min_qv_usdt: float,
                                              min_last_price: float) -> List[str]:
    markets = ex.load_markets(reload=True)
    tickers = ex.fetch_tickers(params={"type": "swap"})
    selected = []
    for sym, m in markets.items():
        try:
            if m.get("type") != "swap" or not m.get("linear"): continue
            if m.get("settle") != "USDT" or m.get("quote") != "USDT": continue
            base = m.get("base", "")
            if any(tag in base for tag in ["UP","DOWN","3L","3S","4L","4S"]): continue
            t = tickers.get(sym, {})
            qv = float(t.get("quoteVolume") or 0.0)
            last = float(t.get("last") or 0.0)
            if qv < min_qv_usdt or last < min_last_price: continue
            selected.append(sym)
        except: continue
    return selected

def fetch_ohlcv_safe(ex: ccxt.Exchange, symbol: str, timeframe: str, limit: int = 200):
    return ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)

def last_bar_change_pct(ohlcv: list) -> Tuple[float, int, float, float]:
    if not ohlcv or len(ohlcv) < 2: return 0.0, 0, 0.0, 0.0
    prev_close = float(ohlcv[-2][4]); last_close = float(ohlcv[-1][4])
    ts = int(ohlcv[-1][0])
    rsi = compute_rsi([row[4] for row in ohlcv], 14)
    return (last_close/prev_close - 1.0)*100.0, ts, last_close, rsi

# ------------------------- RSI -------------------------

def compute_rsi(closes: List[float], length: int = 14) -> float:
    if len(closes) < length+1: return 50.0
    gains = []; losses = []
    for i in range(-length, 0):
        change = closes[i] - closes[i-1]
        if change >= 0: gains.append(change)
        else: losses.append(-change)
    avg_gain = sum(gains)/length if gains else 0
    avg_loss = sum(losses)/length if losses else 0
    if avg_loss == 0: return 100.0
    rs = avg_gain/avg_loss
    return 100 - (100/(1+rs))

def rsi_state(rsi: float) -> str:
    if rsi >= 70: return f"⚠️ Перегрето (RSI={rsi:.1f})"
    if rsi <= 30: return f"⚠️ Перепроданность (RSI={rsi:.1f})"
    return f"ℹ️ Нейтрально (RSI={rsi:.1f})"

# ------------------------- Форматирование -------------------------

def format_stats_block(stats: Optional[Dict[str,float]], direction: str) -> str:
    if not stats: return "История: данных пока мало."
    hdr = "История похожих всплесков:" if direction=="pump" else "История похожих дампов:"
    lines = [hdr, f"— эпизодов: <b>{stats['episodes']}</b>"]
    if stats.get("avg_revert_min") is not None:
        lines.append(f"— ср. время до {'отката' if direction=='pump' else 'отскока'}: <b>{stats['avg_revert_min']:.0f} мин</b>")
    if stats.get("avg_min_60m") is not None:
        lines.append(f"— худший ход: <b>{stats['avg_min_60m']:.2f}%</b>")
    if stats.get("avg_max_60m") is not None:
        lines.append(f"— лучший ход: <b>{stats['avg_max_60m']:.2f}%</b>")
    return "\n".join(lines)

# ------------------------- Основной цикл -------------------------

def main():
    print("Инициализация...")
    init_db()
    fut = ex_swap()
    fut_syms = pick_all_swap_usdt_symbols_with_liquidity(fut, MIN_24H_QUOTE_VOLUME_USDT, MIN_LAST_PRICE_USDT)
    send_telegram(f"✅ Бот запущен. Контрактов к мониторингу: {len(fut_syms)}")

    while True:
        for timeframe, pump_thr, dump_thr in TIMEFRAMES:
            for sym in fut_syms:
                try:
                    ohlcv = fetch_ohlcv_safe(fut, sym, timeframe, limit=200)
                    chg, ts_ms, close, rsi = last_bar_change_pct(ohlcv)
                    if ts_ms == 0: continue
                    key_symbol = f"FUT:{sym}"

                    # 🚨 Памп
                    if chg >= pump_thr:
                        insert_spike(key_symbol, timeframe, "pump", ts_ms, close)
                        stats = recent_symbol_stats(key_symbol, timeframe, "pump")
                        send_telegram(
                            f"🚨 <b>Памп</b> ({timeframe})\n"
                            f"Контракт: <b>{sym}</b>\n"
                            f"Рост: <b>{chg:.2f}%</b>\n"
                            f"Свеча: {ts_to_iso(ts_ms)}\n\n"
                            f"{rsi_state(rsi)}\n\n"
                            f"{format_stats_block(stats,'pump')}\n\n"
                            f"<i>Не финсовет</i>"
                        )

                    # 🔻 Дамп
                    if chg <= -dump_thr:
                        insert_spike(key_symbol, timeframe, "dump", ts_ms, close)
                        stats = recent_symbol_stats(key_symbol, timeframe, "dump")
                        send_telegram(
                            f"🔻 <b>Дамп</b> ({timeframe})\n"
                            f"Контракт: <b>{sym}</b>\n"
                            f"Падение: <b>{chg:.2f}%</b>\n"
                            f"Свеча: {ts_to_iso(ts_ms)}\n\n"
                            f"{rsi_state(rsi)}\n\n"
                            f"{format_stats_block(stats,'dump')}\n\n"
                            f"<i>Не финсовет</i>"
                        )

                except Exception as e:
                    print(f"[SCAN] {sym} {timeframe}: {e}")
                    time.sleep(0.05)

        time.sleep(POLL_INTERVAL_SEC)

if __name__ == "__main__":
    try: main()
    except KeyboardInterrupt: print("Остановка")
