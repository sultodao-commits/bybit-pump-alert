#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bybit Futures → Telegram (ONLY 15m LONG/SHORT signals, hardcoded config)
RSI + BB + EMA(side+slope) + Volume Z-score + Candle confirm + Cooldown
Свеча в сообщении: UTC и Екатеринбург (UTC+5).
"""

import os
import time
import math
import sqlite3
import traceback
from datetime import datetime, timezone, timedelta
from typing import List, Tuple, Optional

import requests
import ccxt

# ========================= ЖЁСТКАЯ КОНФИГУРАЦИЯ (БЕЗ .env) =========================
# !!! ВСТАВЬ СВОИ ДАННЫЕ НИЖЕ !!!
TELEGRAM_BOT_TOKEN = 8400967954:AAEFTwuOP66NXwD85LTtR0xonUwCrrTi2t0
TELEGRAM_CHAT_ID   = 911511438

# Частота опроса биржи
POLL_INTERVAL_SEC = 30

# Фильтры ликвидности (отбор контрактов)
MIN_24H_QUOTE_VOLUME_USDT = 300000.0
MIN_LAST_PRICE_USDT       = 0.002

# Индикаторы (как в Pine)
LEN_RSI = 14
LEN_EMA = 50
LEN_BB  = 20
BB_MULT = 2.0

RSI_OB  = 70.0     # перекупленность
RSI_OS  = 30.0     # перепроданность

# Логика сигналов
REQUIRE_RETURN_BB = 1   # 1=нужен возврат внутрь BB; 0=достаточно касания
USE_EMA_SIDE      = 1   # 1=цена должна быть на стороне EMA
USE_EMA_SLOPE     = 1   # 1=наклон EMA должен совпадать с направлением
NEED_CANDLE_CONF  = 1   # 1=свечное подтверждение (телесность)
MIN_BODY_PCT      = 0.40  # 0..1

# Объёмный фильтр (15m Z-score)
USE_VOLUME_CONFIRM = 1
VOL_Z_MIN          = 1.0

# Кулдаун (в 15m барах) между одинаковыми сигналами по одному контракту
COOLDOWN_BARS = 5

# Хранилище состояния
STATE_DB = os.path.join(os.path.dirname(__file__), "state_15m_ls.db")

# Таймфрейм
TF = "15m"
TF_MINUTES = 15

# ========================= УТИЛИТЫ =========================

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def ts_dual(ts_ms: int) -> str:
    dt_utc = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    dt_ekb = dt_utc + timedelta(hours=5)  # UTC+5
    return f"{dt_utc.strftime('%Y-%m-%d %H:%M UTC')} | {dt_ekb.strftime('%Y-%m-%d %H:%M ЕКБ')}"

def send_telegram(text: str) -> None:
    assert TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID and TELEGRAM_BOT_TOKEN != "PASTE_YOUR_TELEGRAM_BOT_TOKEN", \
        "Вставь TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в начале файла"
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        r = requests.post(url, json=payload, timeout=12)
        if r.status_code != 200:
            print(f"[TG] HTTP {r.status_code}: {r.text}")
    except Exception as e:
        print(f"[TG] Exception: {e}")

# ========================= БД =========================

def init_db() -> None:
    con = sqlite3.connect(STATE_DB); cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS signals_15m (
            key_symbol TEXT NOT NULL,
            direction  TEXT NOT NULL,   -- 'LONG' | 'SHORT'
            candle_ts  INTEGER NOT NULL,
            PRIMARY KEY (key_symbol, direction, candle_ts)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sig15_symdir ON signals_15m(key_symbol, direction, candle_ts)")
    con.commit(); con.close()

def last_signal_ts(key_symbol: str, direction: str) -> Optional[int]:
    con = sqlite3.connect(STATE_DB); cur = con.cursor()
    cur.execute("""
        SELECT candle_ts FROM signals_15m
        WHERE key_symbol=? AND direction=?
        ORDER BY candle_ts DESC LIMIT 1
    """, (key_symbol, direction))
    row = cur.fetchone(); con.close()
    return int(row[0]) if row else None

def save_signal(key_symbol: str, direction: str, candle_ts: int) -> None:
    con = sqlite3.connect(STATE_DB); cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO signals_15m(key_symbol, direction, candle_ts) VALUES(?,?,?)",
                (key_symbol, direction, int(candle_ts)))
    con.commit(); con.close()

# ========================= БИРЖА =========================

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

# ========================= ИНДИКАТОРЫ (СЕРИИ) =========================

def ema_series(values: List[float], length: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None]*len(values)
    if not values: return out
    k = 2.0 / (length + 1.0)
    e = values[0]
    for i, v in enumerate(values):
        if i == 0: e = v
        else: e = v * k + e * (1.0 - k)
        out[i] = e
    return out

def rsi_series(values: List[float], length: int) -> List[Optional[float]]:
    n = len(values)
    out: List[Optional[float]] = [None]*n
    if n <= length: return out
    gains = [0.0]*(n-1); losses = [0.0]*(n-1)
    for i in range(1, n):
        d = values[i] - values[i-1]
        gains[i-1] = max(d, 0.0)
        losses[i-1] = max(-d, 0.0)
    avg_gain = sum(gains[:length]) / length
    avg_loss = sum(losses[:length]) / length
    out[length] = 100.0 if avg_loss == 0 else (lambda rs: 100.0 - 100.0/(1.0+rs))(avg_gain/avg_loss)
    for i in range(length+1, n):
        avg_gain = (avg_gain*(length-1) + gains[i-1]) / length
        avg_loss = (avg_loss*(length-1) + losses[i-1]) / length
        out[i] = 100.0 if avg_loss == 0 else (lambda rs: 100.0 - 100.0/(1.0+rs))(avg_gain/avg_loss)
    return out

def bb_bands(values: List[float], length: int, mult: float):
    n = len(values)
    basis = [None]*n; upper = [None]*n; lower = [None]*n
    for i in range(n):
        if i+1 < length: continue
        win = values[i+1-length:i+1]
        mean = sum(win) / length
        var = sum((x-mean)*(x-mean) for x in win) / length
        std = math.sqrt(var)
        basis[i] = mean
        upper[i] = mean + mult * std
        lower[i] = mean - mult * std
    return basis, upper, lower

def vol_zscore_last(vols: List[float], window: int) -> Optional[float]:
    if len(vols) < window + 1: return None
    last = vols[-1]
    base = vols[-(window+1):-1]
    mean = sum(base)/len(base)
    var = sum((v-mean)*(v-mean) for v in base)/len(base)
    std = math.sqrt(var)
    if std == 0: return 0.0
    return (last - mean) / std

# ========================= ЛОГИКА СИГНАЛОВ 15m =========================

def decide_long_short_from_last_bar(ohlcv: List[List[float]]) -> Tuple[Optional[str], Optional[str]]:
    """
    На последней закрытой 15m-свече.
    Возвращает ('LONG'/'SHORT'/None, reason_str or None)
    """
    need_len = max(LEN_BB, LEN_EMA, LEN_RSI) + 5
    if not ohlcv or len(ohlcv) < need_len:
        return None, None

    opens  = [float(x[1]) for x in ohlcv]
    highs  = [float(x[2]) for x in ohlcv]
    lows   = [float(x[3]) for x in ohlcv]
    closes = [float(x[4]) for x in ohlcv]
    vols   = [float(x[5]) for x in ohlcv]

    i  = len(ohlcv) - 1
    ip = i - 1
    if ip < 1:
        return None, None

    ema = ema_series(closes, LEN_EMA)
    rsi = rsi_series(closes, LEN_RSI)
    basis, upper, lower = bb_bands(closes, LEN_BB, BB_MULT)

    c = closes[i]; o = opens[i]; h = highs[i]; l = lows[i]
    cp = closes[ip]
    r  = rsi[i]; rp = rsi[ip]
    lo = lower[i]; hi = upper[i]
    lop = lower[ip]; hip = upper[ip]
    e  = ema[i]; e3 = ema[i-3] if i-3 >= 0 else None

    if any(v is None for v in [r, rp, lo, hi, lop, hip, e, e3]):
        return None, None

    # RSI кроссы
    longRSI_cross  = (rp <= RSI_OS) and (r > RSI_OS)
    shortRSI_cross = (rp >= RSI_OB) and (r < RSI_OB)

    # BB touch/return
    touchLow  = (c <= lo) or (l <= lo)
    touchHigh = (c >= hi) or (h >= hi)
    retLong   = (cp <= lop) and (c > lo)
    retShort  = (cp >= hip) and (c < hi)

    trigLongBB  = retLong if REQUIRE_RETURN_BB else touchLow
    trigShortBB = retShort if REQUIRE_RETURN_BB else touchHigh

    longRaw  = longRSI_cross  or trigLongBB
    shortRaw = shortRSI_cross or trigShortBB

    # Свеча (телесность)
    rng  = max(h - l, 1e-12)
    body = abs(c - o)
    bodyPct = body / rng
    bullOk = (c > o) and (bodyPct >= MIN_BODY_PCT)
    bearOk = (c < o) and (bodyPct >= MIN_BODY_PCT)
    candlePassLong  = (not NEED_CANDLE_CONF) or bullOk
    candlePassShort = (not NEED_CANDLE_CONF) or bearOk

    # Сторона к EMA + наклон
    sideLong  = (not USE_EMA_SIDE)  or (c >= e)
    sideShort = (not USE_EMA_SIDE)  or (c <= e)
    slopeUp   = (e3 is not None) and (e - e3 > 0.0)
    slopeDn   = (e3 is not None) and (e - e3 < 0.0)
    trendLong = (not USE_EMA_SLOPE) or slopeUp
    trendShort= (not USE_EMA_SLOPE) or slopeDn

    # Объёмный фильтр (15m)
    volZ = vol_zscore_last(vols, LEN_BB)
    volPass = (not USE_VOLUME_CONFIRM) or (volZ is not None and volZ >= VOL_Z_MIN)

    # Итог
    longOk_pre  = longRaw  and candlePassLong  and sideLong  and trendLong  and volPass
    shortOk_pre = shortRaw and candlePassShort and sideShort and trendShort and volPass

    longOk  = longOk_pre
    shortOk = (not longOk) and shortOk_pre  # при совпадении — приоритет LONG

    if longOk:
        slope_txt = 'UP' if slopeUp else ('FLAT' if (e - e3) == 0 else 'DN')
        reason = f"RSI={r:.1f} | VolZ={volZ:.2f}" if volZ is not None else f"RSI={r:.1f} | VolZ=n/a"
        reason += f" | EMA_slope={slope_txt}"
        return "LONG", reason

    if shortOk:
        slope_txt = 'DN' if slopeDn else ('FLAT' if (e - e3) == 0 else 'UP')
        reason = f"RSI={r:.1f} | VolZ={volZ:.2f}" if volZ is not None else f"RSI={r:.1f} | VolZ=n/a"
        reason += f" | EMA_slope={slope_txt}"
        return "SHORT", reason

    return None, None

# ========================= ГЛАВНЫЙ ЦИКЛ =========================

def main():
    print("Инициализация...")
    init_db()

    ex = ex_swap()
    try:
        syms = pick_all_swap_usdt_symbols_with_liquidity(ex, MIN_24H_QUOTE_VOLUME_USDT, MIN_LAST_PRICE_USDT)
        send_telegram(
            "✅ Бот запущен (Bybit Futures 15m LONG/SHORT, hardcoded)\n"
            f"К мониторингу: <b>{len(syms)}</b>\n"
            f"RSI_OB/OS={RSI_OB}/{RSI_OS} | BB={LEN_BB}/{BB_MULT}\n"
            f"EMA: len={LEN_EMA}, side={USE_EMA_SIDE}, slope={USE_EMA_SLOPE}\n"
            f"VolZ: use={USE_VOLUME_CONFIRM}, min={VOL_Z_MIN}\n"
            f"Candle: need={NEED_CANDLE_CONF}, body≥{MIN_BODY_PCT}\n"
            f"Cooldown: {COOLDOWN_BARS}×15m\n"
            f"Poll: {POLL_INTERVAL_SEC}s"
            .replace(",", " ")
        )
    except Exception as e:
        print(f"[SYMBOLS] Ошибка подбора: {e}")
        traceback.print_exc()
        syms = []

    while True:
        cycle_start = time.time()
        try:
            for sym in syms:
                key_symbol = f"FUT:{sym}"
                try:
                    ohlcv = fetch_ohlcv_safe(ex, sym, timeframe=TF, limit=max(200, LEN_EMA+LEN_BB+30))
                    if not ohlcv or len(ohlcv) < 60:
                        continue

                    last_ts = int(ohlcv[-1][0])

                    # Кулдаун
                    cd_ms = COOLDOWN_BARS * TF_MINUTES * 60 * 1000
                    ls_long  = last_signal_ts(key_symbol, "LONG")
                    ls_short = last_signal_ts(key_symbol, "SHORT")

                    side, reason = decide_long_short_from_last_bar(ohlcv)

                    if side == "LONG":
                        if ls_long is None or (last_ts - ls_long) >= cd_ms:
                            save_signal(key_symbol, "LONG", last_ts)
                            send_telegram(
                                f"🟢 <b>LONG (15m)</b>\n"
                                f"Контракт: <b>{sym}</b>\n"
                                f"Свеча: {ts_dual(last_ts)}\n"
                                f"{reason if reason else ''}"
                            )

                    elif side == "SHORT":
                        if ls_short is None or (last_ts - ls_short) >= cd_ms:
                            save_signal(key_symbol, "SHORT", last_ts)
                            send_telegram(
                                f"🔴 <b>SHORT (15m)</b>\n"
                                f"Контракт: <b>{sym}</b>\n"
                                f"Свеча: {ts_dual(last_ts)}\n"
                                f"{reason if reason else ''}"
                            )

                    time.sleep(0.02)

                except ccxt.RateLimitExceeded:
                    time.sleep(1.0)
                except Exception as e:
                    print(f"[SCAN] {sym}: {e}")
                    time.sleep(0.02)

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
