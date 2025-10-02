#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bybit Futures Alerts → Telegram
(Pumps + Dumps 5m/15m, FAST 1m, RSI(1m) обзор, История/пост-эффект/реверт, Daily Report)

— Только Futures USDT (линейные перпеты, ccxt options.defaultType="swap")
— Основные сигналы: 5m/15m (последняя свеча к предыдущей)
— FAST-сигналы: 1m резкий памп/дамп
— Второе сообщение к каждому алерту: "Ситуация на минутке" с RSI(1m)
— Время свечи в сообщениях — по Екатеринбургу (UTC+5)
— Без дедупликации (сигналы могут повторяться)
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

# Пороги пампов/дампов (рост/падение, % за свечу)
THRESH_5M_PCT   = float(os.getenv("THRESH_5M_PCT", "6"))
THRESH_15M_PCT  = float(os.getenv("THRESH_15M_PCT", "12"))
THRESH_5M_DROP_PCT  = float(os.getenv("THRESH_5M_DROP_PCT", "6"))
THRESH_15M_DROP_PCT = float(os.getenv("THRESH_15M_DROP_PCT", "12"))

# FAST 1m (мгновенный сигнал)
FAST_1M_PUMP_PCT = float(os.getenv("FAST_1M_PUMP_PCT", "5"))
FAST_1M_DUMP_PCT = float(os.getenv("FAST_1M_DUMP_PCT", "5"))

# Фильтры ликвидности
MIN_24H_QUOTE_VOLUME_USDT = float(os.getenv("MIN_24H_QUOTE_VOLUME_USDT", "500000"))
MIN_LAST_PRICE_USDT       = float(os.getenv("MIN_LAST_PRICE_USDT", "0.002"))

# Дневной отчёт — час UTC
DAILY_REPORT_HOUR_UTC = int(os.getenv("DAILY_REPORT_HOUR_UTC", "6"))

# Горизонт для пост-эффекта/реверта
POST_EFFECT_MINUTES = 60

# История для агрегатов (дней)
HISTORY_LOOKBACK_DAYS = int(os.getenv("HISTORY_LOOKBACK_DAYS", "30"))

# Длина RSI для минутной «ситуации»
RSI_LEN_1M = int(os.getenv("RSI_LEN_1M", "14"))

STATE_DB = os.path.join(os.path.dirname(__file__), "state.db")

TIMEFRAMES = [
    ("5m",  THRESH_5M_PCT,  THRESH_5M_DROP_PCT),
    ("15m", THRESH_15M_PCT, THRESH_15M_DROP_PCT),
]
FAST_TIMEFRAME = "1m"

# ------------------------- Время/утилиты -------------------------

EKB_TZ = timezone(timedelta(hours=5))  # UTC+5

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def ts_to_ekb(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=EKB_TZ).strftime("%Y-%m-%d %H:%M:%S ЕКБ")

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
            key_symbol TEXT NOT NULL,      -- 'FUT:BTC/USDT:USDT'
            timeframe  TEXT NOT NULL,      -- '1m'|'5m'|'15m'
            direction  TEXT NOT NULL,      -- 'pump'|'dump'
            candle_ts  INTEGER NOT NULL,   -- ms
            price      REAL NOT NULL,      -- close на событии
            -- пост-эффект:
            min_return_60m REAL,
            max_return_60m REAL,
            fwd_5m REAL, fwd_15m REAL, fwd_30m REAL, fwd_60m REAL,
            revert_min INTEGER,
            evaluated INTEGER DEFAULT 0
        )
    """)
    try:
        cur.execute("CREATE INDEX IF NOT EXISTS idx_spikes_key_tf_dir ON spikes_v2(key_symbol, timeframe, direction)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_spikes_eval_ts ON spikes_v2(evaluated, candle_ts)")
    except Exception:
        pass
    cur.execute("""CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)""")
    con.commit()
    con.close()

def insert_spike(key_symbol: str, timeframe: str, direction: str, candle_ts: int, price: float) -> None:
    con = sqlite3.connect(STATE_DB); cur = con.cursor()
    cur.execute("""INSERT INTO spikes_v2(key_symbol,timeframe,direction,candle_ts,price) VALUES(?,?,?,?,?)""",
                (key_symbol, timeframe, direction, int(candle_ts), float(price)))
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
    """, (min_return_60m, max_return_60m, f5, f15, f30, f60, revert_min,
          key_symbol, timeframe, direction, int(candle_ts)))
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

# ------------------------- Bybit Futures (ccxt) -------------------------

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
    if not ohlcv or len(ohlcv) < 2:
        return 0.0, 0, 0.0
    prev_close = float(ohlcv[-2][4])
    last_close = float(ohlcv[-1][4])
    ts = int(ohlcv[-1][0])
    if prev_close == 0:
        return 0.0, ts, last_close
    return (last_close / prev_close - 1.0) * 100.0, ts, last_close

# ------------------------- Пост-эффект/реверт -------------------------

def _tf_to_minutes(tf: str) -> int:
    if tf.endswith("m"):
        return int(tf[:-1])
    if tf.endswith("h"):
        return int(tf[:-1]) * 60
    raise ValueError("Unsupported timeframe: " + tf)

def compute_post_effect_and_revert(
    ex: ccxt.Exchange, symbol: str, timeframe: str, spike_ts: int, spike_price: float,
    horizon_min: int = POST_EFFECT_MINUTES, direction: str = "pump"
) -> Optional[Tuple[float, float, Optional[float], Optional[float], Optional[float], Optional[float], Optional[int]]]:
    tf_min = _tf_to_minutes(timeframe)
    horizon_bars = max(1, horizon_min // tf_min)
    ohlcv = fetch_ohlcv_safe(ex, symbol, timeframe=timeframe, limit=500)
    if not ohlcv:
        return None
    idx = None
    for i in range(len(ohlcv)):
        if int(ohlcv[i][0]) == spike_ts:
            idx = i; break
    if idx is None:
        return None
    end = min(len(ohlcv) - 1, idx + horizon_bars)
    if end <= idx:
        return None

    closes = [float(row[4]) for row in ohlcv[idx:end+1]]
    if len(closes) > 1:
        min_price = min(closes[1:])
        max_price = max(closes[1:])
    else:
        min_price = max_price = closes[0]

    min_return_60m = (min_price / spike_price - 1.0) * 100.0
    max_return_60m = (max_price / spike_price - 1.0) * 100.0

    def fwd(delta_min: int) -> Optional[float]:
        bars = max(1, delta_min // tf_min)
        j = idx + bars
        if j < len(ohlcv):
            return (float(ohlcv[j][4]) / spike_price - 1.0) * 100.0
        return None

    f5, f15, f30, f60 = fwd(5), fwd(15), fwd(30), fwd(60)

    revert_min: Optional[int] = None
    for j in range(idx + 1, end + 1):
        c = float(ohlcv[j][4])
        if direction == "pump" and c < spike_price:
            revert_min = (j - idx) * tf_min; break
        if direction == "dump" and c > spike_price:
            revert_min = (j - idx) * tf_min; break

    return (min_return_60m, max_return_60m, f5, f15, f30, f60, revert_min)

# ------------------------- RSI(1m) для "ситуации" -------------------------

def calc_rsi_from_closes(closes: List[float], length: int = 14) -> Optional[float]:
    n = len(closes)
    if n < length + 1:
        return None
    gains = []
    losses = []
    for i in range(1, length + 1):
        ch = closes[-i] - closes[-i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    avg_gain = sum(gains) / length
    avg_loss = sum(losses) / length
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return max(0.0, min(100.0, rsi))

def minute_context_text(ex: ccxt.Exchange, symbol: str) -> str:
    try:
        ohlcv = fetch_ohlcv_safe(ex, symbol, timeframe="1m", limit=RSI_LEN_1M + 2)
        closes = [float(x[4]) for x in ohlcv]
        rsi = calc_rsi_from_closes(closes, RSI_LEN_1M)
        rsi_txt = f"{rsi:.1f}%" if rsi is not None else "н/д"
        return (f"#МОНЕТА: <b>{symbol.split(':')[0]}</b>\n"
                f"Ситуация на минутке\n\n"
                f"📊 RSI(1m,{RSI_LEN_1M}): <b>{rsi_txt}</b>\n"
                f"$ Биржа: <b>Bybit</b>")
    except Exception as e:
        print(f"[1m CONTEXT] {symbol}: {e}")
        return (f"#МОНЕТА: <b>{symbol.split(':')[0]}</b>\n"
                f"Ситуация на минутке\n\n"
                f"📊 RSI(1m): н/д\n"
                f"$ Биржа: <b>Bybit</b>")

# ------------------------- Форматирование -------------------------

def format_stats_block(stats: Optional[Dict[str, float]], direction: str) -> str:
    if not stats or stats.get("episodes", 0) == 0:
        return "История: данных пока мало."
    hdr = "История похожих всплесков (до 60м):" if direction == "pump" else "История похожих дампов (до 60м):"
    lines = [hdr, f"— эпизодов: <b>{stats['episodes']}</b>"]
    if stats.get("avg_revert_min") is not None:
        tag = "отката" if direction == "pump" else "отскока"
        lines.append(f"— ср. время до {tag}: <b>{stats['avg_revert_min']:.0f} мин</b>")
    if stats.get("avg_min_60m") is not None:
        lines.append(f"— ср. худший ход: <b>{stats['avg_min_60m']:.2f}%</b>")
    if stats.get("avg_max_60m") is not None:
        lines.append(f"— ср. лучший ход: <b>{stats['avg_max_60m']:.2f}%</b>")
    if stats.get("avg_fwd_5m")  is not None: lines.append(f"— ср. через 5м: <b>{stats['avg_fwd_5m']:.2f}%</b>")
    if stats.get("avg_fwd_15m") is not None: lines.append(f"— ср. через 15м: <b>{stats['avg_fwd_15m']:.2f}%</b>")
    if stats.get("avg_fwd_30m") is not None: lines.append(f"— ср. через 30м: <b>{stats['avg_fwd_30m']:.2f}%</b>")
    if stats.get("avg_fwd_60m") is not None: lines.append(f"— ср. через 60м: <b>{stats['avg_fwd_60m']:.2f}%</b>")
    return "\n".join(lines)

# ------------------------- Дневной отчёт -------------------------

def maybe_daily_report() -> None:
    try:
        utc = now_utc()
        if utc.hour != DAILY_REPORT_HOUR_UTC:
            return
        today = utc.strftime("%Y-%m-%d")
        if meta_get("daily_report_date") == today:
            return

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

    send_telegram(
        "✅ Бот запущен (Bybit Futures; Pump/Dump + FAST 1m + RSI(1m)).\n"
        f"Фильтры: объём ≥ {int(MIN_24H_QUOTE_VOLUME_USDT):,} USDT, цена ≥ {MIN_LAST_PRICE_USDT} USDT.\n"
        f"Пороги: 5m≥{THRESH_5M_PCT}%, 15m≥{THRESH_15M_PCT}% | 5m≤-{THRESH_5M_DROP_PCT}%, 15m≤-{THRESH_15M_DROP_PCT}%.\n"
        f"FAST(1m): +≥{FAST_1M_PUMP_PCT}% / -≥{FAST_1M_DUMP_PCT}%."
        .replace(",", " ")
    )

    fut = ex_swap()
    try:
        fut_syms = pick_all_swap_usdt_symbols_with_liquidity(fut, MIN_24H_QUOTE_VOLUME_USDT, MIN_LAST_PRICE_USDT)
        send_telegram(f"📊 К мониторингу отобрано Futures контрактов: <b>{len(fut_syms)}</b>")
    except Exception as e:
        print(f"[SYMBOLS] Ошибка подбора пар: {e}")
        traceback.print_exc()
        fut_syms = []

    while True:
        cycle_start = time.time()
        try:
            # Дневной отчёт
            maybe_daily_report()

            # Пост-эффект и реверты по прошедшим событиям
            try:
                for key_symbol, timeframe, direction, candle_ts, price in get_unevaluated_spikes(older_than_min=5):
                    try:
                        sym_ccxt = key_symbol.split(":", 1)[1]
                        res = compute_post_effect_and_revert(
                            fut, sym_ccxt, timeframe, candle_ts, price,
                            horizon_min=POST_EFFECT_MINUTES, direction=direction
                        )
                        if res:
                            min60, max60, f5, f15, f30, f60, rev = res
                            update_spike_outcomes_by_ts(
                                key_symbol, timeframe, direction, candle_ts,
                                min60, max60, f5, f15, f30, f60, rev
                            )
                            time.sleep(0.03)
                    except ccxt.RateLimitExceeded:
                        time.sleep(1.5)
                    except Exception as e:
                        print(f"[POST] {key_symbol} {timeframe}: {e}")
                        time.sleep(0.03)
            except Exception as e:
                print(f"[POST-LOOP] Ошибка: {e}")

            # --- Основные таймфреймы 5m/15m
            for timeframe, pump_thr, dump_thr in TIMEFRAMES:
                for sym in fut_syms:
                    key_symbol = f"FUT:{sym}"
                    try:
                        ohlcv = fetch_ohlcv_safe(fut, sym, timeframe=timeframe, limit=200)
                        chg, ts_ms, close = last_bar_change_pct(ohlcv)
                        if ts_ms == 0:
                            continue

                        # 🚨 Памп
                        if chg >= pump_thr:
                            insert_spike(key_symbol, timeframe, "pump", ts_ms, close)
                            stats = recent_symbol_stats(key_symbol, timeframe, "pump")
                            send_telegram(
                                f"🚨 <b>Pump</b> (Futures, {timeframe})\n"
                                f"Контракт: <b>{sym}</b>\n"
                                f"Рост последней свечи: <b>{chg:.2f}%</b>\n"
                                f"Свеча: {ts_to_ekb(ts_ms)}\n\n"
                                f"{format_stats_block(stats, 'pump')}\n\n"
                                f"<i>Не финсовет. Риски на вас.</i>"
                            )
                            # второй пост — «ситуация на минутке»
                            send_telegram(minute_context_text(fut, sym))

                        # 🔻 Дамп
                        if chg <= -dump_thr:
                            insert_spike(key_symbol, timeframe, "dump", ts_ms, close)
                            stats = recent_symbol_stats(key_symbol, timeframe, "dump")
                            send_telegram(
                                f"🔻 <b>Dump</b> (Futures, {timeframe})\n"
                                f"Контракт: <b>{sym}</b>\n"
                                f"Падение последней свечи: <b>{chg:.2f}%</b>\n"
                                f"Свеча: {ts_to_ekb(ts_ms)}\n\n"
                                f"{format_stats_block(stats, 'dump')}\n\n"
                                f"<i>Не финсовет. Риски на вас.</i>"
                            )
                            # второй пост — «ситуация на минутке»
                            send_telegram(minute_context_text(fut, sym))

                    except ccxt.RateLimitExceeded:
                        time.sleep(2.0)
                    except Exception as e:
                        print(f"[SCAN {timeframe}] {sym}: {e}")
                        time.sleep(0.03)

            # --- FAST 1m
            for sym in fut_syms:
                key_symbol = f"FUT:{sym}"
                try:
                    ohlcv = fetch_ohlcv_safe(fut, sym, timeframe=FAST_TIMEFRAME, limit=200)
                    chg, ts_ms, close = last_bar_change_pct(ohlcv)
                    if ts_ms == 0:
                        continue

                    # FAST Pump
                    if chg >= FAST_1M_PUMP_PCT:
                        insert_spike(key_symbol, "1m", "pump", ts_ms, close)
                        send_telegram(
                            f"🔥 <b>Pump FAST</b> (Futures, 1m)\n"
                            f"Контракт: <b>{sym}</b>\n"
                            f"Рост 1m: <b>{chg:.2f}%</b>\n"
                            f"Свеча: {ts_to_ekb(ts_ms)}\n\n"
                            f"<i>Не финсовет. Риски на вас.</i>"
                        )
                        send_telegram(minute_context_text(fut, sym))

                    # FAST Dump
                    if chg <= -FAST_1M_DUMP_PCT:
                        insert_spike(key_symbol, "1m", "dump", ts_ms, close)
                        send_telegram(
                            f"⚡️ <b>Dump FAST</b> (Futures, 1m)\n"
                            f"Контракт: <b>{sym}</b>\n"
                            f"Падение 1m: <b>{chg:.2f}%</b>\n"
                            f"Свеча: {ts_to_ekb(ts_ms)}\n\n"
                            f"<i>Не финсовет. Риски на вас.</i>"
                        )
                        send_telegram(minute_context_text(fut, sym))

                except ccxt.RateLimitExceeded:
                    time.sleep(2.0)
                except Exception as e:
                    print(f"[FAST 1m] {sym}: {e}")
                    time.sleep(0.03)

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
