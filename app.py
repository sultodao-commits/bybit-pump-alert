#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bybit Alerts → Telegram (Spot + Futures, Pumps + Dumps, History, Daily Report)

Функции:
- Мониторинг Bybit Spot USDT и Futures USDT (линейные перпеты, defaultType="swap")
- Сигналы: 🚨 Памп (рост) и 🔻 Дамп (падение) на 5m/15m (по последней свече таймфрейма)
- Фильтры ликвидности по 24h объёму и минимальной цене
- История: сохраняем событие и считаем пост-эффект (min/max за 60м, fwd 5/15/30/60м)
- В алертах показываем свежую агрегированную статистику по той же монете/рынку/ТФ/направлению
- Дневной отчёт 1 раз в сутки по UTC

Запуск в Scalingo как worker: Procfile → `worker: python app.py`
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

# Пороги пампа (рост, % за одну свечу соответствующего ТФ)
THRESH_5M_PCT   = float(os.getenv("THRESH_5M_PCT", "6"))
THRESH_15M_PCT  = float(os.getenv("THRESH_15M_PCT", "12"))

# Пороги дампа (падение, абсолютные проценты; сравнение идёт chg <= -THRESH_*)
THRESH_5M_DROP_PCT  = float(os.getenv("THRESH_5M_DROP_PCT", "6"))
THRESH_15M_DROP_PCT = float(os.getenv("THRESH_15M_DROP_PCT", "12"))

# Фильтры ликвидности
MIN_24H_QUOTE_VOLUME_USDT = float(os.getenv("MIN_24H_QUOTE_VOLUME_USDT", "500000"))
MIN_LAST_PRICE_USDT       = float(os.getenv("MIN_LAST_PRICE_USDT", "0.002"))

# Дневной отчёт (час UTC, по умолчанию 06:00 UTC ~ 12:00 Азия/Алматы)
DAILY_REPORT_HOUR_UTC = int(os.getenv("DAILY_REPORT_HOUR_UTC", "6"))

# Горизонт пост-эффекта (минут)
POST_EFFECT_MINUTES = 60

# Сколько дней назад брать историю в агрегированной статистике
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
        print(f"[TG] Exception: {e}")

# ------------------------- База данных (v2 схемы) -------------------------

def init_db() -> None:
    con = sqlite3.connect(STATE_DB)
    cur = con.cursor()
    # де-дупликация сигналов (включая направление)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS alerts_v2 (
            key_symbol TEXT NOT NULL,     -- 'SPOT:BTC/USDT' или 'FUT:BTC/USDT:USDT'
            timeframe  TEXT NOT NULL,     -- '5m' | '15m'
            direction  TEXT NOT NULL,     -- 'pump' | 'dump'
            candle_ts  INTEGER NOT NULL,  -- ms
            PRIMARY KEY (key_symbol, timeframe, direction, candle_ts)
        )
    """)
    # история событий и пост-эффекта
    cur.execute("""
        CREATE TABLE IF NOT EXISTS spikes_v2 (
            key_symbol TEXT NOT NULL,
            timeframe  TEXT NOT NULL,
            direction  TEXT NOT NULL,     -- 'pump' | 'dump'
            candle_ts  INTEGER NOT NULL,  -- ms
            price      REAL NOT NULL,     -- close на свече события
            -- пост-эффект в горизонте (по close):
            min_return_60m REAL,          -- минимальная доходность (наихудшая просадка) за 60м, % к цене события
            max_return_60m REAL,          -- максимальная доходность (наилучший отскок) за 60м, % к цене события
            fwd_5m REAL, fwd_15m REAL, fwd_30m REAL, fwd_60m REAL,
            evaluated INTEGER DEFAULT 0,  -- 0 = не считали пост-эффект; 1 = посчитали
            PRIMARY KEY (key_symbol, timeframe, direction, candle_ts)
        )
    """)
    # метаданные (для дневного отчёта)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    con.commit()
    con.close()

def was_alerted(key_symbol: str, timeframe: str, direction: str, candle_ts: int) -> bool:
    con = sqlite3.connect(STATE_DB)
    cur = con.cursor()
    cur.execute("SELECT 1 FROM alerts_v2 WHERE key_symbol=? AND timeframe=? AND direction=? AND candle_ts=?",
                (key_symbol, timeframe, direction, candle_ts))
    row = cur.fetchone()
    con.close()
    return row is not None

def save_alert(key_symbol: str, timeframe: str, direction: str, candle_ts: int) -> None:
    con = sqlite3.connect(STATE_DB)
    cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO alerts_v2(key_symbol, timeframe, direction, candle_ts) VALUES(?,?,?,?)",
                (key_symbol, timeframe, direction, candle_ts))
    con.commit()
    con.close()

def insert_spike(key_symbol: str, timeframe: str, direction: str, candle_ts: int, price: float) -> None:
    con = sqlite3.connect(STATE_DB)
    cur = con.cursor()
    cur.execute("""
        INSERT OR IGNORE INTO spikes_v2(key_symbol, timeframe, direction, candle_ts, price)
        VALUES (?,?,?,?,?)
    """, (key_symbol, timeframe, direction, candle_ts, float(price)))
    con.commit()
    con.close()

def update_spike_outcomes(key_symbol: str, timeframe: str, direction: str, candle_ts: int,
                          min_return_60m: float, max_return_60m: float,
                          fwd5: Optional[float], fwd15: Optional[float],
                          fwd30: Optional[float], fwd60: Optional[float]) -> None:
    con = sqlite3.connect(STATE_DB)
    cur = con.cursor()
    cur.execute("""
        UPDATE spikes_v2
        SET min_return_60m=?, max_return_60m=?, fwd_5m=?, fwd_15m=?, fwd_30m=?, fwd_60m=?, evaluated=1
        WHERE key_symbol=? AND timeframe=? AND direction=? AND candle_ts=?
    """, (min_return_60m, max_return_60m, fwd5, fwd15, fwd30, fwd60, key_symbol, timeframe, direction, candle_ts))
    con.commit()
    con.close()

def get_unevaluated_spikes(older_than_min: int = 5) -> List[Tuple[str, str, str, int, float]]:
    """
    Берём всплески/дампы, которые ещё не оценены и где прошло >= older_than_min минут.
    Возвращает: key_symbol, timeframe, direction, candle_ts, price
    """
    cutoff_ms = int((now_utc() - timedelta(minutes=older_than_min)).timestamp() * 1000)
    con = sqlite3.connect(STATE_DB)
    cur = con.cursor()
    cur.execute("""
        SELECT key_symbol, timeframe, direction, candle_ts, price
        FROM spikes_v2
        WHERE evaluated=0 AND candle_ts <= ?
        ORDER BY candle_ts ASC
    """, (cutoff_ms,))
    rows = cur.fetchall()
    con.close()
    return rows

def recent_symbol_stats(key_symbol: str, timeframe: str, direction: str, days: int = HISTORY_LOOKBACK_DAYS) -> Optional[Dict[str, float]]:
    """
    Агрегаты по похожим событиям (та же монета/рынок, тот же ТФ, то же направление) за последние N дней.
    """
    since_ms = int((now_utc() - timedelta(days=days)).timestamp() * 1000)
    con = sqlite3.connect(STATE_DB)
    cur = con.cursor()
    cur.execute("""
        SELECT min_return_60m, max_return_60m, fwd_5m, fwd_15m, fwd_30m, fwd_60m
        FROM spikes_v2
        WHERE key_symbol=? AND timeframe=? AND direction=? AND evaluated=1 AND candle_ts>=?
    """, (key_symbol, timeframe, direction, since_ms))
    rows = cur.fetchall()
    con.close()
    if not rows:
        return None
    def avg(vals):
        vals = [v for v in vals if v is not None]
        return sum(vals)/len(vals) if vals else None
    min60 = [r[0] for r in rows if r[0] is not None]
    max60 = [r[1] for r in rows if r[1] is not None]
    f5  = [r[2] for r in rows if r[2] is not None]
    f15 = [r[3] for r in rows if r[3] is not None]
    f30 = [r[4] for r in rows if r[4] is not None]
    f60 = [r[5] for r in rows if r[5] is not None]
    stats = {
        "episodes": len(rows),
        "avg_min_60m": avg(min60),
        "avg_max_60m": avg(max60),
        "avg_fwd_5m":  avg(f5),
        "avg_fwd_15m": avg(f15),
        "avg_fwd_30m": avg(f30),
        "avg_fwd_60m": avg(f60),
    }
    return stats

def meta_get(key: str) -> Optional[str]:
    con = sqlite3.connect(STATE_DB)
    cur = con.cursor()
    cur.execute("SELECT value FROM meta WHERE key=?", (key,))
    row = cur.fetchone()
    con.close()
    return row[0] if row else None

def meta_set(key: str, value: str) -> None:
    con = sqlite3.connect(STATE_DB)
    cur = con.cursor()
    cur.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)", (key, value))
    con.commit()
    con.close()

# ------------------------- Bybit (ccxt) -------------------------

def ex_spot() -> ccxt.bybit:
    return ccxt.bybit({
        "enableRateLimit": True,
        "timeout": 20000,
        "options": {"defaultType": "spot"},
    })

def ex_swap() -> ccxt.bybit:
    # Линейные перпеты USDT
    return ccxt.bybit({
        "enableRateLimit": True,
        "timeout": 20000,
        "options": {"defaultType": "swap"},
    })

def pick_all_spot_usdt_symbols_with_liquidity(ex: ccxt.Exchange,
                                              min_qv_usdt: float,
                                              min_last_price: float) -> List[str]:
    markets = ex.load_markets(reload=True)
    tickers = ex.fetch_tickers(params={"type": "spot"})
    selected = []
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
            if qv < min_qv_usdt or last < min_last_price:
                continue
            selected.append(sym)
        except Exception:
            continue
    return selected

def pick_all_swap_usdt_symbols_with_liquidity(ex: ccxt.Exchange,
                                              min_qv_usdt: float,
                                              min_last_price: float) -> List[str]:
    """
    Берём линейные USDT-перпеты: m['type']=='swap' и m['linear']==True, settle='USDT', quote='USDT'
    Символы вида 'BTC/USDT:USDT'
    """
    markets = ex.load_markets(reload=True)
    tickers = ex.fetch_tickers(params={"type": "swap"})
    selected = []
    for sym, m in markets.items():
        try:
            if m.get("type") != "swap" or not m.get("swap"):
                continue
            if not m.get("linear"):
                continue
            if m.get("settle") != "USDT":
                continue
            if m.get("quote") != "USDT":
                continue
            base = m.get("base", "")
            if any(tag in base for tag in ["UP", "DOWN", "3L", "3S", "4L", "4S"]):
                continue
            t = tickers.get(sym, {})
            qv = float(t.get("quoteVolume") or t.get("baseVolume") or 0.0)
            last = float(t.get("last") or t.get("close") or 0.0)
            if qv < min_qv_usdt or last < min_last_price:
                continue
            selected.append(sym)
        except Exception:
            continue
    return selected

def fetch_ohlcv_safe(ex: ccxt.Exchange, symbol: str, timeframe: str, limit: int = 200):
    return ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)

def last_bar_change_pct(ohlcv: list) -> Tuple[float, int, float]:
    """
    Возвращает (chg%, ts_ms, close) по последней свече vs предыдущая.
    """
    if not ohlcv or len(ohlcv) < 2:
        return 0.0, 0, 0.0
    prev_close = float(ohlcv[-2][4])
    last_close = float(ohlcv[-1][4])
    ts = int(ohlcv[-1][0])
    if prev_close == 0:
        return 0.0, ts, last_close
    return (last_close / prev_close - 1.0) * 100.0, ts, last_close

# ------------------------- Пост-эффект события -------------------------

def compute_post_effect(ex: ccxt.Exchange, symbol: str, timeframe: str,
                        spike_ts: int, spike_price: float, horizon_min: int = POST_EFFECT_MINUTES
                        ) -> Optional[Tuple[float, float, Optional[float], Optional[float], Optional[float], Optional[float]]]:
    """
    Считает:
      - min_return_60m (наихудшая просадка)
      - max_return_60m (наилучший отскок)
      - fwd 5/15/30/60m
    Возвращает (min60, max60, f5, f15, f30, f60)
    """
    tf = timeframe
    tf_min = int(tf[:-1]) if tf.endswith("m") else int(tf[:-1]) * 60
    horizon_bars = max(1, horizon_min // tf_min)
    ohlcv = fetch_ohlcv_safe(ex, symbol, timeframe=tf, limit=500)
    if not ohlcv:
        return None

    idx = None
    for i in range(len(ohlcv)):
        if int(ohlcv[i][0]) == spike_ts:
            idx = i
            break
    if idx is None:
        return None

    end = min(len(ohlcv) - 1, idx + horizon_bars)
    if end <= idx:
        return None

    closes = [float(row[4]) for row in ohlcv[idx:end+1]]
    if not closes:
        return None

    min_price = min(closes[1:]) if len(closes) > 1 else closes[0]
    max_price = max(closes[1:]) if len(closes) > 1 else closes[0]

    min_return_60m = (min_price / spike_price - 1.0) * 100.0
    max_return_60m = (max_price / spike_price - 1.0) * 100.0

    def fwd(delta_min: int) -> Optional[float]:
        bars = max(1, delta_min // tf_min)
        j = idx + bars
        if j < len(ohlcv):
            return (float(ohlcv[j][4]) / spike_price - 1.0) * 100.0
        return None

    return (min_return_60m, max_return_60m, fwd(5), fwd(15), fwd(30), fwd(60))

# ------------------------- Форматирование сообщений -------------------------

def format_stats_block(stats: Optional[Dict[str, float]], direction: str) -> str:
    if not stats or stats.get("episodes", 0) == 0:
        return "История: данных пока мало."
    header = "История похожих всплесков (до 60м):" if direction == "pump" else "История похожих дампов (до 60м):"
    lines = [header, f"— эпизодов: <b>{stats['episodes']}</b>"]
    if stats.get("avg_min_60m") is not None:
        lines.append(f"— ср. худший ход: <b>{stats['avg_min_60m']:.2f}%</b>")
    if stats.get("avg_max_60m") is not None:
        lines.append(f"— ср. лучший ход: <b>{stats['avg_max_60m']:.2f}%</b>")
    if stats.get("avg_fwd_5m") is not None:
        lines.append(f"— ср. через 5м: <b>{stats['avg_fwd_5m']:.2f}%</b>")
    if stats.get("avg_fwd_15m") is not None:
        lines.append(f"— ср. через 15м: <b>{stats['avg_fwd_15m']:.2f}%</b>")
    if stats.get("avg_fwd_30m") is not None:
        lines.append(f"— ср. через 30м: <b>{stats['avg_fwd_30m']:.2f}%</b>")
    if stats.get("avg_fwd_60m") is not None:
        lines.append(f"— ср. через 60м: <b>{stats['avg_fwd_60m']:.2f}%</b>")
    return "\n".join(lines)

# ------------------------- Дневной отчёт -------------------------

def maybe_daily_report() -> None:
    try:
        utc = now_utc()
        today = utc.strftime("%Y-%m-%d")
        if utc.hour != DAILY_REPORT_HOUR_UTC:
            return
        if meta_get("daily_report_date") == today:
            return

        since_ms = int((utc - timedelta(hours=24)).timestamp() * 1000)
        con = sqlite3.connect(STATE_DB)
        cur = con.cursor()
        cur.execute("""
            SELECT direction, COUNT(*)
            FROM spikes_v2
            WHERE candle_ts >= ?
            GROUP BY direction
        """, (since_ms,))
        rows = cur.fetchall()
        con.close()

        pumps = next((cnt for d, cnt in rows if d == "pump"), 0)
        dumps = next((cnt for d, cnt in rows if d == "dump"), 0)
        msg = (f"📅 Дневной отчёт (последние 24ч)\n"
               f"— Пампов: <b>{pumps}</b>\n"
               f"— Дампов: <b>{dumps}</b>\n"
               f"— Время (UTC): {utc.strftime('%Y-%m-%d %H:%M')}")
        send_telegram(msg)
        meta_set("daily_report_date", today)
    except Exception as e:
        print(f"[REPORT] Ошибка: {e}")
        traceback.print_exc()

# ------------------------- Основной цикл -------------------------

def main():
    print("Инициализация...")
    init_db()

    try:
        send_telegram(
            "✅ Бот запущен (Bybit Spot + Futures; пампы и дампы).\n"
            f"Фильтры: объём ≥ {int(MIN_24H_QUOTE_VOLUME_USDT):,} USDT, цена ≥ {MIN_LAST_PRICE_USDT} USDT.\n"
            f"Пороги: Pumps 5m≥{THRESH_5M_PCT}%, 15m≥{THRESH_15M_PCT}% | Dumps 5m≤-{THRESH_5M_DROP_PCT}%, 15m≤-{THRESH_15M_DROP_PCT}%."
            .replace(",", " ")
        )
    except Exception as e:
        print(f"[BOOT PING] TG error: {e}")

    spot = ex_spot()
    fut  = ex_swap()

    try:
        spot_syms = pick_all_spot_usdt_symbols_with_liquidity(spot, MIN_24H_QUOTE_VOLUME_USDT, MIN_LAST_PRICE_USDT)
        fut_syms  = pick_all_swap_usdt_symbols_with_liquidity(fut,  MIN_24H_QUOTE_VOLUME_USDT, MIN_LAST_PRICE_USDT)
        send_telegram(f"📊 К мониторингу отобрано: Spot <b>{len(spot_syms)}</b>, Futures <b>{len(fut_syms)}</b> пар.")
    except Exception as e:
        print(f"[SYMBOLS] Ошибка подбора пар: {e}")
        traceback.print_exc()
        spot_syms, fut_syms = [], []

    while True:
        cycle_start = time.time()
        try:
            # 1) Дневной отчёт
            maybe_daily_report()

            # 2) Досчитать пост-эффект для прошлых событий (ждём минимум 5 минут)
            try:
                pending = get_unevaluated_spikes(older_than_min=5)
                for key_symbol, timeframe, direction, candle_ts, price in pending:
                    market = "SPOT" if key_symbol.startswith("SPOT:") else "FUT"
                    ex = spot if market == "SPOT" else fut
                    try:
                        res = compute_post_effect(ex, key_symbol.split(":",1)[1], timeframe, candle_ts, price)
                        if res:
                            min60, max60, f5, f15, f30, f60 = res
                            update_spike_outcomes(key_symbol, timeframe, direction, candle_ts, min60, max60, f5, f15, f30, f60)
                            time.sleep(0.05)
                    except ccxt.RateLimitExceeded:
                        time.sleep(2)
                    except Exception as e:
                        print(f"[POST] Ошибка {key_symbol} {timeframe}: {e}")
                        traceback.print_exc()
                        time.sleep(0.05)
            except Exception as e:
                print(f"[POST] Общая ошибка: {e}")
                traceback.print_exc()

            # 3) Мониторинг новых событий
            for timeframe, pump_thr, dump_thr in TIMEFRAMES:
                # --- Spot
                for sym in spot_syms:
                    key_symbol = f"SPOT:{sym}"
                    try:
                        ohlcv = fetch_ohlcv_safe(spot, sym, timeframe=timeframe, limit=200)
                        chg, ts_ms, close = last_bar_change_pct(ohlcv)
                        if ts_ms == 0:
                            continue

                        # Памп
                        if chg >= pump_thr and not was_alerted(key_symbol, timeframe, "pump", ts_ms):
                            insert_spike(key_symbol, timeframe, "pump", ts_ms, close)
                            save_alert(key_symbol, timeframe, "pump", ts_ms)
                            stats = recent_symbol_stats(key_symbol, timeframe, "pump")
                            msg = (
                                f"🚨 <b>Памп</b> (Spot, {timeframe})\n"
                                f"Монета: <b>{sym}</b>\n"
                                f"Рост последней свечи: <b>{chg:.2f}%</b>\n"
                                f"Свеча: {ts_to_iso(ts_ms)}\n\n"
                                f"{format_stats_block(stats, 'pump')}\n\n"
                                f"<i>Не финсовет. Проверяйте ликвидность и риски.</i>"
                            )
                            send_telegram(msg)
                            time.sleep(0.15)

                        # Дамп
                        if chg <= -dump_thr and not was_alerted(key_symbol, timeframe, "dump", ts_ms):
                            insert_spike(key_symbol, timeframe, "dump", ts_ms, close)
                            save_alert(key_symbol, timeframe, "dump", ts_ms)
                            stats = recent_symbol_stats(key_symbol, timeframe, "dump")
                            msg = (
                                f"🔻 <b>Дамп</b> (Spot, {timeframe})\n"
                                f"Монета: <b>{sym}</b>\n"
                                f"Падение последней свечи: <b>{chg:.2f}%</b>\n"
                                f"Свеча: {ts_to_iso(ts_ms)}\n\n"
                                f"{format_stats_block(stats, 'dump')}\n\n"
                                f"<i>Не финсовет. Проверяйте ликвидность и риски.</i>"
                            )
                            send_telegram(msg)
                            time.sleep(0.15)

                    except ccxt.RateLimitExceeded:
                        time.sleep(3)
                    except Exception as e:
                        print(f"[SPOT] Ошибка {sym} {timeframe}: {e}")
                        traceback.print_exc()
                        time.sleep(0.05)

                # --- Futures (перпеты)
                for sym in fut_syms:
                    key_symbol = f"FUT:{sym}"
                    try:
                        ohlcv = fetch_ohlcv_safe(fut, sym, timeframe=timeframe, limit=200)
                        chg, ts_ms, close = last_bar_change_pct(ohlcv)
                        if ts_ms == 0:
                            continue

                        # Памп
                        if chg >= pump_thr and not was_alerted(key_symbol, timeframe, "pump", ts_ms):
                            insert_spike(key_symbol, timeframe, "pump", ts_ms, close)
                            save_alert(key_symbol, timeframe, "pump", ts_ms)
                            stats = recent_symbol_stats(key_symbol, timeframe, "pump")
                            msg = (
                                f"🚨 <b>Памп</b> (Futures, {timeframe})\n"
                                f"Контракт: <b>{sym}</b>\n"
                                f"Рост последней свечи: <b>{chg:.2f}%</b>\n"
                                f"Свеча: {ts_to_iso(ts_ms)}\n\n"
                                f"{format_stats_block(stats, 'pump')}\n\n"
                                f"<i>Не финсовет. Проверяйте ликвидность и риски.</i>"
                            )
                            send_telegram(msg)
                            time.sleep(0.15)

                        # Дамп
                        if chg <= -dump_thr and not was_alerted(key_symbol, timeframe, "dump", ts_ms):
                            insert_spike(key_symbol, timeframe, "dump", ts_ms, close)
                            save_alert(key_symbol, timeframe, "dump", ts_ms)
                            stats = recent_symbol_stats(key_symbol, timeframe, "dump")
                            msg = (
                                f"🔻 <b>Дамп</b> (Futures, {timeframe})\n"
                                f"Контракт: <b>{sym}</b>\n"
                                f"Падение последней свечи: <b>{chg:.2f}%</b>\n"
                                f"Свеча: {ts_to_iso(ts_ms)}\n\n"
                                f"{format_stats_block(stats, 'dump')}\n\n"
                                f"<i>Не финсовет. Проверяйте ликвидность и риски.</i>"
                            )
                            send_telegram(msg)
                            time.sleep(0.15)

                    except ccxt.RateLimitExceeded:
                        time.sleep(3)
                    except Exception as e:
                        print(f"[FUT] Ошибка {sym} {timeframe}: {e}")
                        traceback.print_exc()
                        time.sleep(0.05)

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
