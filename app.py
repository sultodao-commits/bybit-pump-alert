#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bybit Futures Alerts → Telegram (Pump/Dump, History, Revert, Side Hint)
Улучшенная версия с мгновенными сигналами на откат
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

# ========================= Конфигурация =========================

load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "").strip()
assert TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, "Укажи TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID"

POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", "60"))

# Пороги пампов/дампов (% за свечу сигнального ТФ)
THRESH_5M_PCT   = float(os.getenv("THRESH_5M_PCT", "6"))
THRESH_15M_PCT  = float(os.getenv("THRESH_15M_PCT", "12"))
THRESH_5M_DROP_PCT  = float(os.getenv("THRESH_5M_DROP_PCT", "6"))
THRESH_15M_DROP_PCT = float(os.getenv("THRESH_15M_DROP_PCT", "12"))

# Ликвидность
MIN_24H_QUOTE_VOLUME_USDT = float(os.getenv("MIN_24H_QUOTE_VOLUME_USDT", "500000"))
MIN_LAST_PRICE_USDT       = float(os.getenv("MIN_LAST_PRICE_USDT", "0.002"))

# Пост-эффект
POST_EFFECT_MINUTES = 60
HISTORY_LOOKBACK_DAYS = int(os.getenv("HISTORY_LOOKBACK_DAYS", "30"))

# Параметры сигналов отката
REVERSION_EXTREME_MULT = float(os.getenv("REVERSION_EXTREME_MULT", "1.8"))
RSI_EXTREME_OB = float(os.getenv("RSI_EXTREME_OB", "78"))
RSI_EXTREME_OS = float(os.getenv("RSI_EXTREME_OS", "22"))

STATE_DB = os.path.join(os.path.dirname(__file__), "state.db")

TIMEFRAMES = [
    ("5m",  THRESH_5M_PCT,  THRESH_5M_DROP_PCT),
    ("15m", THRESH_15M_PCT, THRESH_15M_DROP_PCT),
]

# ========================= Время/утилиты =========================

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def ts_dual(ts_ms: int) -> str:
    dt_utc = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    dt_ekb = dt_utc + timedelta(hours=5)  # Екатеринбург = UTC+5
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

# ========================= База данных =========================

def init_db() -> None:
    con = sqlite3.connect(STATE_DB); cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS spikes_v2 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key_symbol TEXT NOT NULL,
            timeframe  TEXT NOT NULL,
            direction  TEXT NOT NULL,      -- 'pump' | 'dump'
            candle_ts  INTEGER NOT NULL,   -- ms
            price      REAL NOT NULL,      -- close на событии
            min_return_60m REAL,
            max_return_60m REAL,
            fwd_5m REAL, fwd_15m REAL, fwd_30m REAL, fwd_60m REAL,
            revert_min INTEGER,
            evaluated INTEGER DEFAULT 0
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_spikes_key_tf_dir ON spikes_v2(key_symbol, timeframe, direction)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_spikes_eval_ts ON spikes_v2(evaluated, candle_ts)")
    con.commit(); con.close()

def insert_spike(key_symbol: str, timeframe: str, direction: str, candle_ts: int, price: float) -> None:
    con = sqlite3.connect(STATE_DB); cur = con.cursor()
    cur.execute("""INSERT INTO spikes_v2(key_symbol,timeframe,direction,candle_ts,price)
                   VALUES (?,?,?,?,?)""", (key_symbol, timeframe, direction, int(candle_ts), float(price)))
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
    if not rows: return None

    def avg_ok(arr):
        arr = [x for x in arr if x is not None]
        return (sum(arr)/len(arr)) if arr else None

    return {
        "episodes": len(rows),
        "avg_min_60m":  avg_ok([r[0] for r in rows]),
        "avg_max_60m":  avg_ok([r[1] for r in rows]),
        "avg_fwd_5m":   avg_ok([r[2] for r in rows]),
        "avg_fwd_15m":  avg_ok([r[3] for r in rows]),
        "avg_fwd_30m":  avg_ok([r[4] for r in rows]),
        "avg_fwd_60m":  avg_ok([r[5] for r in rows]),
        "avg_revert_min": avg_ok([r[6] for r in rows]),
    }

# ========================= Биржа (Bybit swap) =========================

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
            if m.get("settle") != "USDT" or m.get("quote") != "USDT": continue
            base = m.get("base", "")
            if any(tag in base for tag in ["UP","DOWN","3L","3S","4L","4S"]): continue
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
    if not ohlcv or len(ohlcv) < 2: return 0.0, 0, 0.0
    prev_close = float(ohlcv[-2][4]); last_close = float(ohlcv[-1][4])
    ts = int(ohlcv[-1][0])
    if prev_close == 0: return 0.0, ts, last_close
    return (last_close/prev_close - 1.0)*100.0, ts, last_close

# ========================= Индикаторы (1m) =========================

def rsi(values: List[float], length: int = 14) -> Optional[float]:
    if len(values) <= length: return None
    gains, losses = [], []
    for i in range(1, len(values)):
        d = values[i] - values[i-1]
        gains.append(max(d, 0.0)); losses.append(max(-d, 0.0))
    avg_gain = sum(gains[:length]) / length
    avg_loss = sum(losses[:length]) / length
    for i in range(length, len(gains)):
        avg_gain = (avg_gain*(length-1) + gains[i]) / length
        avg_loss = (avg_loss*(length-1) + losses[i]) / length
    if avg_loss == 0: return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

def bb(values: List[float], length: int = 20, mult: float = 2.0) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    if len(values) < length: return None, None, None
    window = values[-length:]
    mean = sum(window) / length
    var = sum((x-mean)*(x-mean) for x in window) / length
    std = var ** 0.5
    upper = mean + mult * std
    lower = mean - mult * std
    return mean, upper, lower

def one_min_context(ex: ccxt.Exchange, symbol: str):
    try:
        ohlcv = fetch_ohlcv_safe(ex, symbol, timeframe="1m", limit=100)
        closes = [float(x[4]) for x in ohlcv]
        last_close = closes[-1] if closes else None
        r = rsi(closes, 14)
        _, u, l = bb(closes, 20, 2.0)
        return last_close, r, u, l
    except Exception as e:
        print(f"[1m ctx] {symbol}: {e}")
        return None, None, None, None

def rsi_status_line(r: Optional[float]) -> str:
    if r is None: return "RSI(1m): n/a"
    if r >= RSI_EXTREME_OB:  return f"RSI(1m): <b>{r:.1f}</b> — <b>🔥 ПЕРЕГРЕТО!</b>"
    if r <= RSI_EXTREME_OS:  return f"RSI(1m): <b>{r:.1f}</b> — <b>🧊 ПЕРЕПРОДАНО!</b>"
    if r >= 70: return f"RSI(1m): <b>{r:.1f}</b> — перекупленность"
    if r <= 30: return f"RSI(1m): <b>{r:.1f}</b> — перепроданность"
    return f"RSI(1m): <b>{r:.1f}</b> — нейтрально"

# ========================= СИГНАЛЫ ОТКАТА =========================

def aggressive_reversion_signal(direction: str, chg_pct: float, 
                              rsi_1m: Optional[float], 
                              last_close_1m: Optional[float],
                              upper_bb_1m: Optional[float],
                              lower_bb_1m: Optional[float],
                              pump_thr: float, dump_thr: float) -> Tuple[str, str]:
    """
    Агрессивные сигналы на откат только на явные экстремумы
    Возвращает: (направление, обоснование)
    """
    if rsi_1m is None:
        return "—", "нет данных RSI"
    
    if direction == "pump":
        # СИЛЬНЫЙ ПАМП → ожидаем ОТКАТ (SHORT)
        if chg_pct >= pump_thr * REVERSION_EXTREME_MULT:
            if rsi_1m >= RSI_EXTREME_OB:
                return "SHORT", f"🔥 КРИТИЧЕСКИЙ ПАМП! {chg_pct:.1f}% + RSI {rsi_1m:.1f} - МГНОВЕННЫЙ ОТКАТ!"
            elif rsi_1m >= 75:
                if last_close_1m and upper_bb_1m and last_close_1m > upper_bb_1m:
                    bb_over = (last_close_1m / upper_bb_1m - 1) * 100
                    return "SHORT", f"🚨 Сильный памп {chg_pct:.1f}% + выше BB {bb_over:.1f}% + RSI {rsi_1m:.1f}"
                return "SHORT", f"🚨 Сильный памп {chg_pct:.1f}% + перегрев RSI {rsi_1m:.1f}"
            else:
                return "SHORT", f"⚡ Сильный памп {chg_pct:.1f}% - вероятен откат"
        
        elif chg_pct >= pump_thr:
            if rsi_1m >= 75:
                return "SHORT", f"⚡ Памп {chg_pct:.1f}% + RSI перегрев {rsi_1m:.1f}"
    
    elif direction == "dump":
        # СИЛЬНЫЙ ДАМП → ожидаем ОТСКОК (LONG)
        if chg_pct <= -dump_thr * REVERSION_EXTREME_MULT:
            if rsi_1m <= RSI_EXTREME_OS:
                return "LONG", f"🔥 КРИТИЧЕСКИЙ ДАМП! {chg_pct:.1f}% + RSI {rsi_1m:.1f} - МГНОВЕННЫЙ ОТСКОК!"
            elif rsi_1m <= 25:
                if last_close_1m and lower_bb_1m and last_close_1m < lower_bb_1m:
                    bb_under = (1 - last_close_1m / lower_bb_1m) * 100
                    return "LONG", f"🚨 Сильный дамп {chg_pct:.1f}% + ниже BB {bb_under:.1f}% + RSI {rsi_1m:.1f}"
                return "LONG", f"🚨 Сильный дамп {chg_pct:.1f}% + перепродан RSI {rsi_1m:.1f}"
            else:
                return "LONG", f"⚡ Сильный дамп {chg_pct:.1f}% - вероятен отскок"
        
        elif chg_pct <= -dump_thr:
            if rsi_1m <= 25:
                return "LONG", f"⚡ Дамп {chg_pct:.1f}% + RSI перепродан {rsi_1m:.1f}"
    
    return "—", "нет сильного сигнала"

def format_signal_message(side: str, reason: str) -> str:
    """Форматирование сигнала для Telegram"""
    if side == "—":
        return "➡️ Идея: — (ожидаем развитие движения)"
    
    if side == "SHORT":
        emoji = "📉"
        action = "ОТКАТ после пампа"
    else:
        emoji = "📈" 
        action = "ОТСКОК после дампа"
    
    return f"🎯 <b>СИГНАЛ: {side} {emoji}</b>\n🤔 Прогноз: {action}\n📊 Обоснование: {reason}"

# ========================= Пост-эффект/реверт =========================

def _tf_to_minutes(tf: str) -> int:
    if tf.endswith("m"): return int(tf[:-1])
    if tf.endswith("h"): return int(tf[:-1]) * 60
    raise ValueError("Unsupported timeframe: " + tf)

def compute_post_effect_and_revert(ex: ccxt.Exchange, symbol: str, timeframe: str,
                                   spike_ts: int, spike_price: float,
                                   horizon_min: int = POST_EFFECT_MINUTES,
                                   direction: str = "pump"):
    tf_min = _tf_to_minutes(timeframe)
    horizon_bars = max(1, horizon_min // tf_min)
    ohlcv = fetch_ohlcv_safe(ex, symbol, timeframe=timeframe, limit=500)
    if not ohlcv: return None

    idx = None
    for i in range(len(ohlcv)):
        if int(ohlcv[i][0]) == spike_ts:
            idx = i; break
    if idx is None: return None
    end = min(len(ohlcv)-1, idx + horizon_bars)
    if end <= idx: return None

    closes = [float(r[4]) for r in ohlcv[idx:end+1]]
    if len(closes) > 1:
        min_price = min(closes[1:]); max_price = max(closes[1:])
    else:
        min_price = max_price = closes[0]

    min_return_60m = (min_price/spike_price - 1.0)*100.0
    max_return_60m = (max_price/spike_price - 1.0)*100.0

    def fwd(delta_min: int) -> Optional[float]:
        bars = max(1, delta_min // tf_min)
        j = idx + bars
        if j < len(ohlcv): return (float(ohlcv[j][4])/spike_price - 1.0)*100.0
        return None

    f5, f15, f30, f60 = fwd(5), fwd(15), fwd(30), fwd(60)

    revert_min: Optional[int] = None
    for j in range(idx+1, end+1):
        c = float(ohlcv[j][4])
        if direction=="pump" and c < spike_price: revert_min = (j - idx) * tf_min; break
        if direction=="dump" and c > spike_price: revert_min = (j - idx) * tf_min; break

    return (min_return_60m, max_return_60m, f5, f15, f30, f60, revert_min)

# ========================= Форматирование =========================

def format_stats_block(stats: Optional[Dict[str,float]], direction: str) -> str:
    if not stats or stats.get("episodes",0)==0:
        return "📈 История: данных пока мало."
    hdr = "📈 История похожих ПАМПОВ (до 60м):" if direction=="pump" else "📈 История похожих ДАМПОВ (до 60м):"
    lines = [hdr, f"— эпизодов: <b>{stats['episodes']}</b>"]
    if stats.get("avg_revert_min") is not None:
        lines.append(f"— ср. время до {'отката' if direction=='pump' else 'отскока'}: <b>{stats['avg_revert_min']:.0f} мин</b>")
    if stats.get("avg_min_60m") is not None:
        lines.append(f"— ср. худший ход: <b>{stats['avg_min_60m']:.2f}%</b>")
    if stats.get("avg_max_60m") is not None:
        lines.append(f"— ср. лучший ход: <b>{stats['avg_max_60m']:.2f}%</b>")
    if stats.get("avg_fwd_5m")  is not None: lines.append(f"— ср. через 5м: <b>{stats['avg_fwd_5m']:.2f}%</b>")
    if stats.get("avg_fwd_15m") is not None: lines.append(f"— ср. через 15м: <b>{stats['avg_fwd_15m']:.2f}%</b>")
    if stats.get("avg_fwd_30m") is not None: lines.append(f"— ср. через 30м: <b>{stats['avg_fwd_30m']:.2f}%</b>")
    if stats.get("avg_fwd_60m") is not None: lines.append(f"— ср. через 60м: <b>{stats['avg_fwd_60m']:.2f}%</b>")
    return "\n".join(lines)

# ========================= Основной цикл =========================

def main():
    print("Инициализация...")
    init_db()
    fut = ex_swap()

    try:
        fut_syms = pick_all_swap_usdt_symbols_with_liquidity(fut, MIN_24H_QUOTE_VOLUME_USDT, MIN_LAST_PRICE_USDT)
        send_telegram(
            "✅ Бот запущен (Bybit Futures; сигналы: Памп/Дамп + ОТКАТЫ)\n"
            f"Пороги 5m: Pump ≥ {THRESH_5M_PCT:.2f}% | Dump ≤ -{THRESH_5M_DROP_PCT:.2f}%\n"
            f"Пороги 15m: Pump ≥ {THRESH_15M_PCT:.2f}% | Dump ≤ -{THRESH_15M_DROP_PCT:.2f}%\n"
            f"Сигналы отката: множитель {REVERSION_EXTREME_MULT}x | RSI крит: {RSI_EXTREME_OB}/{RSI_EXTREME_OS}\n"
            f"Опрос: каждые {POLL_INTERVAL_SEC}s\n"
            f"Отобрано контрактов: <b>{len(fut_syms)}</b>"
        )
    except Exception as e:
        print(f"[SYMBOLS] Ошибка подбора: {e}")
        traceback.print_exc()
        fut_syms = []

    while True:
        cycle_start = time.time()
        try:
            # Досчёт пост-эффекта по прошедшим событиям (спустя ≥5 минут)
            try:
                for key_symbol, timeframe, direction, candle_ts, price in get_unevaluated_spikes(older_than_min=5):
                    try:
                        sym_ccxt = key_symbol.split(":", 1)[1]
                        res = compute_post_effect_and_revert(fut, sym_ccxt, timeframe, candle_ts, price,
                                                             horizon_min=POST_EFFECT_MINUTES, direction=direction)
                        if res:
                            min60, max60, f5, f15, f30, f60, rev = res
                            update_spike_outcomes_by_ts(key_symbol, timeframe, direction, candle_ts,
                                                        min60, max60, f5, f15, f30, f60, rev)
                            time.sleep(0.03)
                    except ccxt.RateLimitExceeded:
                        time.sleep(1.5)
                    except Exception as e:
                        print(f"[POST] {key_symbol} {timeframe}: {e}")
                        time.sleep(0.03)
            except Exception as e:
                print(f"[POST-LOOP] Ошибка: {e}")

            # Скан сигналов
            for timeframe, pump_thr, dump_thr in TIMEFRAMES:
                for sym in fut_syms:
                    key_symbol = f"FUT:{sym}"
                    try:
                        ohlcv = fetch_ohlcv_safe(fut, sym, timeframe=timeframe, limit=200)
                        chg, ts_ms, close = last_bar_change_pct(ohlcv)
                        if ts_ms == 0: continue

                        # Контекст 1m
                        last1m, rsi1m, up1m, lo1m = one_min_context(fut, sym)

                        # ---- Памп
                        if chg >= pump_thr:
                            insert_spike(key_symbol, timeframe, "pump", ts_ms, close)
                            stats = recent_symbol_stats(key_symbol, timeframe, "pump")

                            # Агрессивный сигнал на откат
                            side, reason = aggressive_reversion_signal(
                                "pump", chg, rsi1m, last1m, up1m, lo1m, pump_thr, dump_thr
                            )
                            signal_line = format_signal_message(side, reason)

                            send_telegram(
                                f"🚨 <b>ПАМП</b> (Futures, {timeframe})\n"
                                f"Контракт: <b>{sym}</b>\n"
                                f"Рост: <b>{chg:.2f}%</b> 📈\n"
                                f"Свеча: {ts_dual(ts_ms)}\n\n"
                                f"{rsi_status_line(rsi1m)}\n"
                                f"{signal_line}\n\n"
                                f"{format_stats_block(stats,'pump')}\n\n"
                                f"<i>Не финсовет. Риски на вас.</i>"
                            )

                        # ---- Дамп
                        if chg <= -dump_thr:
                            insert_spike(key_symbol, timeframe, "dump", ts_ms, close)
                            stats = recent_symbol_stats(key_symbol, timeframe, "dump")

                            # Агрессивный сигнал на отскок
                            side, reason = aggressive_reversion_signal(
                                "dump", chg, rsi1m, last1m, up1m, lo1m, pump_thr, dump_thr
                            )
                            signal_line = format_signal_message(side, reason)

                            send_telegram(
                                f"🔻 <b>ДАМП</b> (Futures, {timeframe})\n"
                                f"Контракт: <b>{sym}</b>\n"
                                f"Падение: <b>{chg:.2f}%</b> 📉\n"
                                f"Свеча: {ts_dual(ts_ms)}\n\n"
                                f"{rsi_status_line(rsi1m)}\n"
                                f"{signal_line}\n\n"
                                f"{format_stats_block(stats,'dump')}\n\n"
                                f"<i>Не финсовет. Риски на вас.</i>"
                            )

                    except ccxt.RateLimitExceeded:
                        time.sleep(2.0)
                    except Exception as e:
                        print(f"[SCAN] {sym} {timeframe}: {e}")
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
