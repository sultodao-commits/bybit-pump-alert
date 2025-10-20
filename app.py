#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bybit Futures Alerts → Telegram (Pump/Dump, History, Revert, Side Hint)

— Пампы/Дампы на 5m/15m (последняя свеча к предыдущей)
— История + пост-эффект (min/max, fwd 5/15/30/60м, время до реверта)
— Сообщения только двух типов: Памп 🚨 и Дамп 🔻
— В каждом сообщении:
    • RSI(1m) статус (перегрето/перепроданность/нейтрально)
    • ЯВНОЕ направление идеи: ЛОНГ / ШОРТ / —
— Время свечи: UTC и Екатеринбург (UTC+5)
— ФИЛЬТРЫ: только мемкоины и низкая капитализация
"""

import os
import time
import sqlite3
import traceback
import re
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

# Ликвидность - РАСШИРЯЕМ ДИАПАЗОН
MIN_24H_QUOTE_VOLUME_USDT = float(os.getenv("MIN_24H_QUOTE_VOLUME_USDT", "100000"))  # Уменьшили до 100K
MIN_LAST_PRICE_USDT       = float(os.getenv("MIN_LAST_PRICE_USDT", "0.0001"))        # Уменьшили минимальную цену

# ФИЛЬТРЫ ДЛЯ МЕМКОИНОВ И НИЗКОЙ КАПИТАЛИЗАЦИИ - РАСШИРЯЕМ
MAX_MARKET_CAP = float(os.getenv("MAX_MARKET_CAP", "5000000000"))  # 5B max
MIN_MARKET_CAP = float(os.getenv("MIN_MARKET_CAP", "0"))           # Убрали минимальную капу

# Списки мемкоинов (РАСШИРЕННЫЙ СПИСОК)
MEME_KEYWORDS = [
    'DOGE', 'SHIB', 'PEPE', 'FLOKI', 'BONK', 'MEME', 'WIF', 'BOME', 'BABYDOGE',
    'ELON', 'DOG', 'CAT', 'HAM', 'TURBO', 'AIDOGE', 'AISHIB', 'PENGU', 'MOCHI',
    'WOJAK', 'KABOSU', 'KISHU', 'SAMO', 'SNEK', 'POPCAT', 'LILY', 'MOG', 'TOSHI',
    'HIPO', 'CHAD', 'GROK', 'LADYS', 'VOY', 'COQ', 'KERMIT', 'SPX', 'TRUMP',
    'BODEN', 'TREMP', 'SC', 'SMURFCAT', 'ANDY', 'WEN', 'MYRO', 'WU', 'MICHI',
    'NUB', 'DAVE', 'PONKE', 'MON', 'PUDGY', 'POWELL', 'PENG', 'SATOSHI', 'VITALIK',
    'KEVIN', 'OSAK', 'BRETT', 'ZYN', 'TAMA', 'NEIRO', 'NOOT', 'SPUG', 'PIRB',
    'MOUTAI', 'MOG', 'MILADY', 'STAN', 'MOTHER', 'MARTIAN', 'MILK', 'SHIBA', 'AKITA',
    'CORG', 'HUSKY', 'POODL', 'SAITAMA', 'KAI', 'VOLT', 'BEE', 'RABBIT', 'FISH',
    'MOON', 'SUN', 'STAR', 'PLANET', 'ROCKET', 'LAMBO', 'FERRARI', 'TESLA', 'SPACEX',
    'NASA', 'APE', 'BULL', 'BEAR', 'WHALE', 'DOLPHIN', 'TIGER', 'LION', 'FOX',
    'WOLF', 'OWL', 'EAGLE', 'FALCON', 'PHOENIX', 'DRAGON', 'UNICORN', 'ALIEN',
    'ROBOT', 'ANDROID', 'CYBORG', 'NINJA', 'SAMURAI', 'VIKING', 'PIRATE', 'ZOMBIE',
    'VAMPIRE', 'WITCH', 'WIZARD', 'GHOST', 'SKULL', 'BONE', 'FART', 'POOP', 'PEE',
    'CUM', 'ASS', 'BOOB', 'BUTT', 'DICK', 'WEED', 'BEER', 'WINE', 'VODKA', 'WHISKEY',
    'COKE', 'PEPsi', 'COFFEE', 'TEA', 'PIZZA', 'BURGER', 'TACO', 'SUSHI', 'RAMEN',
    'TOAST', 'BAGEL', 'DONUT', 'CAKE', 'COOKIE', 'CANDY', 'CHOCO', 'ICE', 'FIRE',
    'WATER', 'EARTH', 'AIR', 'METAL', 'WOOD', 'GLASS', 'PAPER', 'ROCK', 'SCISSORS',
    'COM', 'NET', 'IO', 'AI', 'APP', 'WEB', 'GAME', 'PLAY', 'FUN', 'LOL', 'LMAO',
    'ROFL', 'HODL', 'FOMO', 'YOLO', 'NGMI', 'WAGMI', 'GM', 'GN', 'GL', 'HF',
    'LFG', 'FREN', 'SER', 'DEJ', 'JTO', 'JUP', 'PYTH', 'RAY', 'ORCA', 'SAMO'
]

# Дополнительные паттерны для мемкоинов
MEME_PATTERNS = [
    re.compile(r'.*DOGE.*', re.IGNORECASE),
    re.compile(r'.*SHIB.*', re.IGNORECASE), 
    re.compile(r'.*PEPE.*', re.IGNORECASE),
    re.compile(r'.*FLOKI.*', re.IGNORECASE),
    re.compile(r'.*BONK.*', re.IGNORECASE),
    re.compile(r'.*MEME.*', re.IGNORECASE),
    re.compile(r'.*BABY.*', re.IGNORECASE),
    re.compile(r'.*ELON.*', re.IGNORECASE),
    re.compile(r'.*DOG.*', re.IGNORECASE),
    re.compile(r'.*CAT.*', re.IGNORECASE),
    re.compile(r'.*PENG.*', re.IGNORECASE),
    re.compile(r'.*WOJAK.*', re.IGNORECASE),
    re.compile(r'.*FROG.*', re.IGNORECASE),
    re.compile(r'.*FISH.*', re.IGNORECASE),
    re.compile(r'.*MOON.*', re.IGNORECASE),
    re.compile(r'.*SUN.*', re.IGNORECASE),
    re.compile(r'.*STAR.*', re.IGNORECASE),
    re.compile(r'.*APE.*', re.IGNORECASE),
    re.compile(r'.*BULL.*', re.IGNORECASE),
    re.compile(r'.*BEAR.*', re.IGNORECASE),
    re.compile(r'.*WHALE.*', re.IGNORECASE),
    re.compile(r'.*HODL.*', re.IGNORECASE),
    re.compile(r'.*FOMO.*', re.IGNORECASE),
    re.compile(r'.*YOLO.*', re.IGNORECASE),
    re.compile(r'.*WAGMI.*', re.IGNORECASE),
    re.compile(r'.*GM.*', re.IGNORECASE),
    re.compile(r'.*LFG.*', re.IGNORECASE),
    re.compile(r'^[A-Z]*DOG[A-Z]*$', re.IGNORECASE),
    re.compile(r'^[A-Z]*CAT[A-Z]*$', re.IGNORECASE),
    re.compile(r'^[A-Z]*MOON[A-Z]*$', re.IGNORECASE),
    re.compile(r'^[A-Z]*APE[A-Z]*$', re.IGNORECASE),
    re.compile(r'^[A-Z]*PEPE[A-Z]*$', re.IGNORECASE),
]

# Пост-эффект
POST_EFFECT_MINUTES = 60
HISTORY_LOOKBACK_DAYS = int(os.getenv("HISTORY_LOOKBACK_DAYS", "30"))

# Параметры подсказки-направления (в .env можно менять)
SIDE_HINT_MULT     = float(os.getenv("SIDE_HINT_MULT", "1.5"))   # импульс сильнее порога во столько раз
RSI_OB             = float(os.getenv("RSI_OB", "70"))            # перекупленность
RSI_OS             = float(os.getenv("RSI_OS", "30"))            # перепроданность
BB_LEN             = int(os.getenv("BB_LEN", "20"))
BB_MULT            = float(os.getenv("BB_MULT", "2.0"))

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

# ========================= ФИЛЬТРЫ МЕМКОИНОВ =========================

def is_meme_coin(symbol: str) -> bool:
    """Проверяет, является ли монета мемкоином"""
    base_symbol = symbol.split('/')[0] if '/' in symbol else symbol
    
    # Проверка по ключевым словам
    for keyword in MEME_KEYWORDS:
        if keyword in base_symbol.upper():
            return True
    
    # Проверка по регулярным выражениям
    for pattern in MEME_PATTERNS:
        if pattern.match(base_symbol):
            return True
    
    # Дополнительные эвристики для мемкоинов
    meme_indicators = [
        base_symbol.upper().startswith('MEME'),
        base_symbol.upper().endswith('DOGE'),
        base_symbol.upper().endswith('SHIB'),
        base_symbol.upper().endswith('PEPE'),
        'DOG' in base_symbol.upper() and len(base_symbol) <= 8,
        'CAT' in base_symbol.upper() and len(base_symbol) <= 8,
        'PEPE' in base_symbol.upper(),
        'FLOKI' in base_symbol.upper(),
        'BONK' in base_symbol.upper(),
        'MOON' in base_symbol.upper() and len(base_symbol) <= 10,
        'APE' in base_symbol.upper() and len(base_symbol) <= 10,
        'HODL' in base_symbol.upper(),
        'FOMO' in base_symbol.upper(),
        'YOLO' in base_symbol.upper(),
    ]
    
    return any(meme_indicators)

def get_market_cap_estimate(ticker_data: Dict) -> Optional[float]:
    """
    Оценка капитализации через объем * цену
    Это приблизительная оценка, так как точная капитализация не всегда доступна
    """
    try:
        last_price = float(ticker_data.get('last', 0))
        base_volume = float(ticker_data.get('baseVolume', 0))
        
        if last_price > 0 and base_volume > 0:
            # Примерная оценка: объем * цена * множитель
            # Это очень грубая оценка! В реальности нужно использовать API с рыночной капой
            estimated_mcap = base_volume * last_price * 5  # уменьшили множитель
            return estimated_mcap
    except Exception:
        pass
    return None

def filter_meme_and_lowcap_symbols(markets: Dict, tickers: Dict, 
                                 min_mcap: float, max_mcap: float,
                                 min_volume: float) -> List[str]:
    """Фильтрует символы по мемкоинам и капитализации"""
    selected = []
    
    for sym, m in markets.items():
        try:
            if m.get("type") != "swap" or not m.get("swap"): continue
            if not m.get("linear"): continue
            if m.get("settle") != "USDT" or m.get("quote") != "USDT": continue
            
            base = m.get("base", "")
            # УБИРАЕМ СЛИШКОМ СТРОГИЕ ФИЛЬТРЫ
            # if any(tag in base for tag in ["UP","DOWN","3L","3S","4L","4S"]): continue
            
            t = tickers.get(sym, {})
            qv = float(t.get("quoteVolume") or 0.0)
            last = float(t.get("last") or 0.0)
            
            if qv < min_volume or last < MIN_LAST_PRICE_USDT: continue
            
            # ФИЛЬТР: Только мемкоины (ОСЛАБЛЯЕМ ФИЛЬТР)
            if not is_meme_coin(sym):
                continue
                
            # ФИЛЬТР: Проверка капитализации (если доступно) - ОСЛАБЛЯЕМ
            estimated_mcap = get_market_cap_estimate(t)
            if estimated_mcap:
                if estimated_mcap > max_mcap:
                    continue
                # УБИРАЕМ ПРОВЕРКУ НА МИНИМАЛЬНУЮ КАПИТАЛИЗАЦИЮ
                # if estimated_mcap < min_mcap:
                #     continue
            
            selected.append(sym)
            
        except Exception:
            continue
    
    return selected

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
    
    # ИСПОЛЬЗУЕМ НОВЫЙ ФИЛЬТР вместо старого
    selected = filter_meme_and_lowcap_symbols(
        markets, tickers, 
        MIN_MARKET_CAP, MAX_MARKET_CAP,
        min_qv_usdt
    )
    
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

def ema(values: List[float], length: int) -> Optional[float]:
    if len(values) < length: return None
    k = 2 / (length + 1.0)
    e = values[-length]
    for v in values[-length+1:]:
        e = v * k + e * (1 - k)
    return e

def bb(values: List[float], length: int = BB_LEN, mult: float = BB_MULT) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    if len(values) < length: return None, None, None
    window = values[-length:]
    mean = sum(window) / length
    var = sum((x-mean)*(x-mean) for x in window) / length
    std = var ** 0.5
    upper = mean + mult * std
    lower = mean - mult * std
    return mean, upper, lower

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

def one_min_context(ex: ccxt.Exchange, symbol: str):
    try:
        ohlcv = fetch_ohlcv_safe(ex, symbol, timeframe="1m", limit=max(200, BB_LEN + 30))
        closes = [float(x[4]) for x in ohlcv]
        last_close = closes[-1] if closes else None
        r = rsi(closes, 14)
        _, u, l = bb(closes, BB_LEN, BB_MULT)
        return last_close, r, u, l
    except Exception as e:
        print(f"[1m ctx] {symbol}: {e}")
        return None, None, None, None

def rsi_status_line(r: Optional[float]) -> str:
    if r is None: return "RSI(1m): n/a"
    if r >= RSI_OB:  return f"RSI(1m): <b>{r:.1f}</b> — <b>перегрето</b>"
    if r <= RSI_OS:  return f"RSI(1m): <b>{r:.1f}</b> — <b>перепроданность</b>"
    return f"RSI(1m): <b>{r:.1f}</b> — нейтрально"

def decide_trade_side(direction: str, chg_pct: float, last_close_1m: Optional[float],
                      upper_bb_1m: Optional[float], lower_bb_1m: Optional[float],
                      rsi_1m: Optional[float], pump_thr: float, dump_thr: float) -> Tuple[str, Optional[str]]:
    """
    Возвращает ("LONG"/"SHORT"/"—", причина|None).
    Эвристика:
      — Pump: если импульс >= SIDE_HINT_MULT * порога и (RSI1m >= RSI_OB или close>upperBB1m) → SHORT
      — Dump: если импульс <= -SIDE_HINT_MULT * порога и (RSI1m <= RSI_OS или close<lowerBB1m) → LONG
      — Иначе: "—"
    """
    try:
        if direction == "pump":
            strong = chg_pct >= pump_thr * SIDE_HINT_MULT
            cond_rsi = (rsi_1m is not None and rsi_1m >= RSI_OB)
            cond_bb  = (upper_bb_1m is not None and last_close_1m is not None and last_close_1m > upper_bb_1m)
            if strong and (cond_rsi or cond_bb):
                reasons = []
                if cond_rsi: reasons.append(f"RSI1m={rsi_1m:.1f}≥{RSI_OB:.0f}")
                if cond_bb:
                    over = (last_close_1m/upper_bb_1m - 1.0)*100.0
                    reasons.append(f"над BB1m {over:.1f}%")
                return "SHORT", ", ".join(reasons) if reasons else None

        if direction == "dump":
            strong = chg_pct <= -dump_thr * SIDE_HINT_MULT
            cond_rsi = (rsi_1m is not None and rsi_1m <= RSI_OS)
            cond_bb  = (lower_bb_1m is not None and last_close_1m is not None and last_close_1m < lower_bb_1m)
            if strong and (cond_rsi or cond_bb):
                reasons = []
                if cond_rsi: reasons.append(f"RSI1m={rsi_1m:.1f}≤{RSI_OS:.0f}")
                if cond_bb:
                    under = (1.0 - last_close_1m/lower_bb_1m)*100.0
                    reasons.append(f"ниже BB1m {under:.1f}%")
                return "LONG", ", ".join(reasons) if reasons else None

        return "—", None
    except Exception:
        return "—", None

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
        return "История: данных пока мало."
    hdr = "История похожих всплесков (до 60м):" if direction=="pump" else "История похожих дампов (до 60м):"
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
        
        # ДОПОЛНИТЕЛЬНАЯ ИНФОРМАЦИЯ О ФИЛЬТРАХ
        meme_count = len([s for s in fut_syms if is_meme_coin(s)])
        
        send_telegram(
            "✅ Бот запущен (Bybit Futures; сигналы: Памп/Дамп)\n"
            f"<b>ФИЛЬТРЫ:</b> Только мемкоины и низкая капитализация\n"
            f"Капитализация: до ${MAX_MARKET_CAP:,.0f}\n"
            f"Минимальный объем: {int(MIN_24H_QUOTE_VOLUME_USDT):,} USDT\n"
            f"Пороги 5m: Pump ≥ {THRESH_5M_PCT:.2f}% | Dump ≤ -{THRESH_5M_DROP_PCT:.2f}%\n"
            f"Пороги 15m: Pump ≥ {THRESH_15M_PCT:.2f}% | Dump ≤ -{THRESH_15M_DROP_PCT:.2f}%\n"
            f"Подсказка: mult={SIDE_HINT_MULT} | RSI_OB/OS={RSI_OB}/{RSI_OS} | BB={BB_LEN}/{BB_MULT}\n"
            f"Опрос: каждые {POLL_INTERVAL_SEC}s\n"
            f"Отобрано контрактов: <b>{len(fut_syms)}</b>"
            .replace(",", " ")
        )
        
        # ВЫВОДИМ СПИСОК НАЙДЕННЫХ МОНЕТ ДЛЯ ДЕБАГА
        print(f"\n=== НАЙДЕНО {len(fut_syms)} МОНЕТ ===")
        for i, sym in enumerate(fut_syms[:50], 1):  # показываем первые 50
            print(f"{i:2d}. {sym}")
        if len(fut_syms) > 50:
            print(f"... и еще {len(fut_syms) - 50} монет")
        print("=======================\n")
        
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

                            side, reason = decide_trade_side("pump", chg, last1m, up1m, lo1m, rsi1m, pump_thr, dump_thr)
                            side_line = f"➡️ Идея: <b>{side}</b>" + (f" ({reason})" if reason else "") if side != "—" else "➡️ Идея: —"

                            # ОБНОВЛЕННОЕ СООБЩЕНИЕ С АКЦЕНТОМ НА МЕМКОИН
                            send_telegram(
                                f"🚨 <b>ПАМП МЕМКОИНА</b> (Futures, {timeframe})\n"
                                f"Контракт: <b>{sym}</b> 🐶\n"
                                f"Рост: <b>{chg:.2f}%</b>\n"
                                f"Свеча: {ts_dual(ts_ms)}\n"
                                f"{rsi_status_line(rsi1m)}\n"
                                f"{side_line}\n\n"
                                f"{format_stats_block(stats,'pump')}\n\n"
                                f"<i>Мемкоин! Высокие риски! Не финсовет.</i>"
                            )

                        # ---- Дамп
                        if chg <= -dump_thr:
                            insert_spike(key_symbol, timeframe, "dump", ts_ms, close)
                            stats = recent_symbol_stats(key_symbol, timeframe, "dump")

                            side, reason = decide_trade_side("dump", chg, last1m, up1m, lo1m, rsi1m, pump_thr, dump_thr)
                            side_line = f"➡️ Идея: <b>{side}</b>" + (f" ({reason})" if reason else "") if side != "—" else "➡️ Идея: —"

                            # ОБНОВЛЕННОЕ СООБЩЕНИЕ С АКЦЕНТОМ НА МЕМКОИН
                            send_telegram(
                                f"🔻 <b>ДАМП МЕМКОИНА</b> (Futures, {timeframe})\n"
                                f"Контракт: <b>{sym}</b> 📉\n"
                                f"Падение: <b>{chg:.2f}%</b>\n"
                                f"Свеча: {ts_dual(ts_ms)}\n"
                                f"{rsi_status_line(rsi1m)}\n"
                                f"{side_line}\n\n"
                                f"{format_stats_block(stats,'dump')}\n\n"
                                f"<i>Мемкоин! Высокие риски! Не финсовет.</i>"
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
