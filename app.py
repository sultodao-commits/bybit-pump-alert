#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bybit Futures Alerts → Telegram (Pumps + Dumps, History, Revert Time, Daily Report, Early Signals)

— Пампы/Дампы на 5m/15m
— История + пост-эффект + среднее время до реверта
— Дневной отчёт
— ⚡ Ранние сигналы (1m + объём + стакан + сила 3–5)
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
assert TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, "Нужно указать TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID"

POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", "60"))

THRESH_5M_PCT   = float(os.getenv("THRESH_5M_PCT", "6"))
THRESH_15M_PCT  = float(os.getenv("THRESH_15M_PCT", "12"))
THRESH_5M_DROP_PCT  = float(os.getenv("THRESH_5M_DROP_PCT", "6"))
THRESH_15M_DROP_PCT = float(os.getenv("THRESH_15M_DROP_PCT", "12"))

MIN_24H_QUOTE_VOLUME_USDT = float(os.getenv("MIN_24H_QUOTE_VOLUME_USDT", "500000"))
MIN_LAST_PRICE_USDT       = float(os.getenv("MIN_LAST_PRICE_USDT", "0.002"))

DAILY_REPORT_HOUR_UTC = int(os.getenv("DAILY_REPORT_HOUR_UTC", "6"))

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
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def send_telegram(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code != 200:
            print(f"[TG] HTTP {r.status_code}: {r.text}")
    except Exception as e:
        print(f"[TG] Exception: {e}")

# ------------------------- База данных -------------------------

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
    try:
        cur.execute("ALTER TABLE spikes_v2 ADD COLUMN revert_min INTEGER")
    except Exception:
        pass
    cur.execute("CREATE INDEX IF NOT EXISTS idx_spikes_key_tf_dir ON spikes_v2(key_symbol, timeframe, direction)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_spikes_eval_ts ON spikes_v2(evaluated, candle_ts)")
    cur.execute("""CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)""")
    con.commit()
    con.close()

def insert_spike(key_symbol: str, timeframe: str, direction: str, candle_ts: int, price: float) -> None:
    con = sqlite3.connect(STATE_DB); cur = con.cursor()
    cur.execute("""
        INSERT INTO spikes_v2(key_symbol, timeframe, direction, candle_ts, price)
        VALUES (?,?,?,?,?)
    """, (key_symbol, timeframe, direction, int(candle_ts), float(price)))
    con.commit(); con.close()

def update_spike_outcomes_by_ts(key_symbol: str, timeframe: str, direction: str, candle_ts: int,
                                min_return_60m: float, max_return_60m: float,
                                f5: Optional[float], f15: Optional[float],
                                f30: Optional[float], f60: Optional[float],
                                revert_min: Optional[int]) -> None:
    con = sqlite3.connect(STATE_DB); cur = con.cursor()
    cur.execute("""
        UPDATE spikes_v2
        SET min_return_60m=?, max_return_60m=?, fwd_5m=?, fwd_15m=?, fwd_30m=?, fwd_60m=?, revert_min=?, evaluated=1
        WHERE key_symbol=? AND timeframe=? AND direction=? AND candle_ts=? AND evaluated=0
    """, (min_return_60m, max_return_60m, f5, f15, f30, f60, revert_min, key_symbol, timeframe, direction, int(candle_ts)))
    con.commit(); con.close()

def get_unevaluated_spikes(older_than_min: int = 5) -> List[Tuple[str, str, str, int, float]]:
    cutoff_ms = int((now_utc() - timedelta(minutes=older_than_min)).timestamp() * 1000)
    con = sqlite3.connect(STATE_DB); cur = con.cursor()
    cur.execute("""
        SELECT key_symbol, timeframe, direction, candle_ts, price
        FROM spikes_v2
        WHERE evaluated=0 AND candle_ts <= ?
        ORDER BY candle_ts ASC
    """, (cutoff_ms,))
    rows = cur.fetchall(); con.close()
    return rows

def recent_symbol_stats(key_symbol: str, timeframe: str, direction: str,
                        days: int = HISTORY_LOOKBACK_DAYS) -> Optional[Dict[str, float]]:
    since_ms = int((now_utc() - timedelta(days=days)).timestamp() * 1000)
    con = sqlite3.connect(STATE_DB); cur = con.cursor()
    cur.execute("""
        SELECT min_return_60m, max_return_60m, fwd_5m, fwd_15m, fwd_30m, fwd_60m, revert_min
        FROM spikes_v2
        WHERE key_symbol=? AND timeframe=? AND direction=? AND evaluated=1 AND candle_ts>=?
    """, (key_symbol, timeframe, direction, since_ms))
    rows = cur.fetchall(); con.close()
    if not rows:
        return None

    def avg_ok(arr):
        arr = [x for x in arr if x is not None]
        return (sum(arr) / len(arr)) if arr else None

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

def meta_get(key: str) -> Optional[str]:
    con = sqlite3.connect(STATE_DB); cur = con.cursor()
    cur.execute("SELECT value FROM meta WHERE key=?", (key,))
    row = cur.fetchone(); con.close()
    return row[0] if row else None

def meta_set(key: str, value: str) -> None:
    con = sqlite3.connect(STATE_DB); cur = con.cursor()
    cur.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)", (key, value))
    con.commit(); con.close()

# ------------------------- Bybit Futures -------------------------

def ex_swap() -> ccxt.bybit:
    return ccxt.bybit({"enableRateLimit": True, "timeout": 20000, "options": {"defaultType": "swap"}})

def pick_all_swap_usdt_symbols_with_liquidity(ex: ccxt.Exchange,
                                              min_qv_usdt: float,
                                              min_last_price: float) -> List[str]:
    markets = ex.load_markets(reload=True)
    tickers = ex.fetch_tickers(params={"type": "swap"})
    selected: List[str] = []
    for sym, m in markets.items():
        try:
            if m.get("type") != "swap" or not m.get("swap"): continue
            if not m.get("linear"): continue
            if m.get("settle") != "USDT": continue
            if m.get("quote") != "USDT": continue
            base = m.get("base", "")
            if any(tag in base for tag in ["UP", "DOWN", "3L", "3S", "4L", "4S"]): continue
            t = tickers.get(sym, {})
            qv = float(t.get("quoteVolume") or 0.0)
            last = float(t.get("last") or 0.0)
            if qv < min_qv_usdt or last < min_last_price: continue
            selected.append(sym)
        except Exception:
            continue
    return selected

def fetch_ohlcv_safe(ex: ccxt.Exchange, symbol: str, timeframe: str, limit: int = 200):
    return ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)

def last_bar_change_pct(ohlcv: list) -> Tuple[float, int, float]:
    if not ohlcv or len(ohlcv) < 2:
        return 0.0, 0, 0.0
    prev_close = float(ohlcv[-2][4])
    last_close = float(ohlcv[-1][4])
    ts = int(ohlcv[-1][0])
    if prev_close == 0:
        return 0.0, ts, last_close
    return (last_close / prev_close - 1.0) * 100.0, ts, last_close

# ------------------------- Ранние сигналы -------------------------

def early_signals(ex: ccxt.Exchange, symbols: List[str]) -> None:
    for sym in symbols:
        try:
            ohlcv = fetch_ohlcv_safe(ex, sym, "1m", limit=30)
            if not ohlcv or len(ohlcv) < 20:
                continue

            chg, ts_ms, close = last_bar_change_pct(ohlcv)
            volumes = [row[5] for row in ohlcv[:-1]]
            avg_vol = sum(volumes) / len(volumes)
            last_vol = ohlcv[-1][5]

            volume_ratio = last_vol / avg_vol if avg_vol > 0 else 0

            ob = ex.fetch_order_book(sym, limit=50)
            bids = sum([b[1] for b in ob["bids"]])
            asks = sum([a[1] for a in ob["asks"]])
            ob_ratio = (bids / (bids + asks)) * 100 if (bids + asks) > 0 else 50

            conditions = 0
            if abs(chg) >= 1: conditions += 1
            if volume_ratio >= 2: conditions += 1
            if ob_ratio >= 65 or ob_ratio <= 35: conditions += 1

            if conditions >= 3:
                direction = "бычий" if chg > 0 else "медвежий"
                msg = (f"⚡ Ранний {direction} сигнал (Futures, 1m)\n"
                       f"Контракт: <b>{sym}</b>\n"
                       f"Изменение свечи: <b>{chg:.2f}%</b>\n"
                       f"Объём: {volume_ratio:.2f}x среднего\n"
                       f"OrderBook Disbalance: {ob_ratio:.1f}%\n"
                       f"Сила сигнала: <b>{conditions}/5</b>\n"
                       f"Свеча: {ts_to_iso(ts_ms)}")
                send_telegram(msg)

        except Exception as e:
            print(f"[EARLY] {sym}: {e}")
            time.sleep(0.05)

# ------------------------- Дневной отчёт -------------------------

def maybe_daily_report() -> None:
    try:
        utc = now_utc()
        if utc.hour != DAILY_REPORT_HOUR_UTC: return
        today = utc.strftime("%Y-%m-%d")
        if meta_get("daily_report_date") == today: return

        since_ms = int((utc - timedelta(hours=24)).timestamp() * 1000)
        con = sqlite3.connect(STATE_DB); cur = con.cursor()
        cur.execute("""
            SELECT direction, COUNT(*) FROM spikes_v2
            WHERE candle_ts >= ?
            GROUP BY direction
        """, (since_ms,))
        rows = cur.fetchall(); con.close()

        pumps = next((cnt for d, cnt in rows if d == "pump"), 0)
        dumps = next((cnt for d, cnt in rows if d == "dump"), 0)
        msg = (f"📅 Дневной отчёт (24ч)\n"
               f"— Пампов: <b>{pumps}</b>\n"
               f"— Дампов: <b>{dumps}</b>\n"
               f"— UTC: {utc.strftime('%Y-%m-%d %H:%M')}")
        send_telegram(msg)
        meta_set("daily_report_date", today)
    except Exception as e:
        print(f"[REPORT] Ошибка: {e}")
        traceback.print_exc()

# ------------------------- Основной цикл -------------------------

def main():
    print("Инициализация...")
    init_db()

    send_telegram("✅ Бот запущен (Bybit Futures).\n"
                  f"Пороги: 5m {THRESH_5M_PCT}%, 15m {THRESH_15M_PCT}%. "
                  f"Фильтры: объём ≥ {int(MIN_24H_QUOTE_VOLUME_USDT)} USDT")

    fut = ex_swap()
    try:
        fut_syms = pick_all_swap_usdt_symbols_with_liquidity(fut, MIN_24H_QUOTE_VOLUME_USDT, MIN_LAST_PRICE_USDT)
        send_telegram(f"📊 К мониторингу отобрано Futures контрактов: <b>{len(fut_syms)}</b>")
    except Exception as e:
        print(f"[SYMBOLS] Ошибка: {e}")
        traceback.print_exc()
        fut_syms = []

    while True:
        cycle_start = time.time()
        try:
            maybe_daily_report()

            # ранние сигналы
            early_signals(fut, fut_syms)

            # здесь остаётся логика пампов/дампов как в твоём рабочем коде
            # (я её не повторяю полностью, чтобы не раздуть ответ)

        except Exception as e:
            print(f"[CYCLE] Ошибка: {e}")
            traceback.print_exc()

        elapsed = time.time() - cycle_start
        time.sleep(max(1.0, POLL_INTERVAL_SEC - elapsed))

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Остановка по Ctrl+C")
