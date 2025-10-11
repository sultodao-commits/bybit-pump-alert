#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bybit Futures Alerts → Telegram (Pump/Dump, History, Revert, Side Hint)
БЕССМЕРТНАЯ ВЕРСИЯ
"""

import os
import time
import sqlite3
import traceback
import signal
import sys
import gc
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

# Пороги пампов/дампов
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

# Параметры подсказки-направления
SIDE_HINT_MULT     = float(os.getenv("SIDE_HINT_MULT", "1.5"))
RSI_OB             = float(os.getenv("RSI_OB", "70"))
RSI_OS             = float(os.getenv("RSI_OS", "30"))
BB_LEN             = int(os.getenv("BB_LEN", "20"))
BB_MULT            = float(os.getenv("BB_MULT", "2.0"))

# Настройки бессмертия
MAX_CONSECUTIVE_ERRORS = int(os.getenv("MAX_CONSECUTIVE_ERRORS", "20"))
RESTART_DELAY = int(os.getenv("RESTART_DELAY", "30"))
MEMORY_CHECK_INTERVAL = int(os.getenv("MEMORY_CHECK_INTERVAL", "100"))

STATE_DB = os.path.join(os.path.dirname(__file__), "state.db")

TIMEFRAMES = [
    ("5m",  THRESH_5M_PCT,  THRESH_5M_DROP_PCT),
    ("15m", THRESH_15M_PCT, THRESH_15M_DROP_PCT),
]

# ========================= Глобальные счетчики =========================

class HealthMonitor:
    def __init__(self):
        self.start_time = time.time()
        self.consecutive_errors = 0
        self.cycles_completed = 0
        self.last_health_check = time.time()
        self.memory_usage = 0
        
    def reset_errors(self):
        self.consecutive_errors = 0
        
    def increment_errors(self):
        self.consecutive_errors += 1
        
    def increment_cycles(self):
        self.cycles_completed += 1
        
    def get_uptime(self):
        return time.time() - self.start_time

health_monitor = HealthMonitor()

# ========================= Обработчики сигналов =========================

def signal_handler(sig, frame):
    """Корректная обработка сигналов остановки"""
    print(f"\nПолучен сигнал {sig}, корректное завершение...")
    send_telegram(f"🔄 Контейнер перезапускается (сигнал {sig})")
    sys.exit(0)  # Docker автоматически перезапустит

def setup_signal_handlers():
    """Настройка обработчиков сигналов"""
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

# ========================= Улучшенные утилиты =========================

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def ts_dual(ts_ms: int) -> str:
    dt_utc = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    dt_ekb = dt_utc + timedelta(hours=5)
    return f"{dt_utc.strftime('%Y-%m-%d %H:%M UTC')} | {dt_ekb.strftime('%Y-%m-%d %H:%M ЕКБ')}"

def send_telegram(text: str, retries: int = 3) -> bool:
    """Улучшенная отправка в Telegram с повторами"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    
    for attempt in range(retries):
        try:
            r = requests.post(url, json=payload, timeout=15)
            if r.status_code == 200:
                return True
            elif r.status_code == 429:  # Rate limit
                time.sleep(2 ** attempt)  # Экспоненциальная задержка
            else:
                print(f"[TG] HTTP {r.status_code}: {r.text}")
        except Exception as e:
            print(f"[TG] Attempt {attempt + 1} failed: {e}")
            if attempt < retries - 1:
                time.sleep(1)
    
    return False

# ========================= Улучшенная работа с БД =========================

def init_db() -> None:
    """Инициализация БД с защитой от блокировок"""
    for attempt in range(3):
        try:
            con = sqlite3.connect(STATE_DB, timeout=30.0)
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
            cur.execute("CREATE INDEX IF NOT EXISTS idx_spikes_key_tf_dir ON spikes_v2(key_symbol, timeframe, direction)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_spikes_eval_ts ON spikes_v2(evaluated, candle_ts)")
            con.commit()
            con.close()
            break
        except sqlite3.OperationalError as e:
            if "locked" in str(e) and attempt < 2:
                time.sleep(1)
                continue
            raise

def db_operation_with_retry(operation, *args, max_retries=3):
    """Выполнение операций с БД с повторными попытками"""
    for attempt in range(max_retries):
        try:
            return operation(*args)
        except sqlite3.OperationalError as e:
            if "locked" in str(e) and attempt < max_retries - 1:
                time.sleep(0.5)
                continue
            raise

def insert_spike(key_symbol: str, timeframe: str, direction: str, candle_ts: int, price: float) -> None:
    def _insert():
        con = sqlite3.connect(STATE_DB, timeout=10.0)
        cur = con.cursor()
        cur.execute("""INSERT INTO spikes_v2(key_symbol,timeframe,direction,candle_ts,price)
                       VALUES (?,?,?,?,?)""", (key_symbol, timeframe, direction, int(candle_ts), float(price)))
        con.commit()
        con.close()
    db_operation_with_retry(_insert)

def maintenance_db():
    """Обслуживание БД для долгой работы"""
    try:
        # Удаляем старые данные
        cutoff_ms = int((now_utc() - timedelta(days=HISTORY_LOOKBACK_DAYS + 7)).timestamp() * 1000)
        con = sqlite3.connect(STATE_DB, timeout=30.0)
        cur = con.cursor()
        cur.execute("DELETE FROM spikes_v2 WHERE candle_ts < ?", (cutoff_ms,))
        
        # Оптимизация БД
        cur.execute("VACUUM")
        cur.execute("PRAGMA optimize")
        con.commit()
        con.close()
        print("✅ DB maintenance completed")
    except Exception as e:
        print(f"❌ DB maintenance failed: {e}")

# ========================= Улучшенная работа с биржей =========================

def ex_swap() -> ccxt.bybit:
    """Создание экземпляра биржи с оптимизацией для долгой работы"""
    return ccxt.bybit({
        "enableRateLimit": True,
        "timeout": 30000,
        "options": {
            "defaultType": "swap",
            "adjustForTimeDifference": True
        },
        "sandbox": False,
        "verbose": False,  # Отключаем логи для экономии памяти
        "rateLimit": 100
    })

def fetch_ohlcv_safe(ex: ccxt.Exchange, symbol: str, timeframe: str, limit: int = 200, max_retries=3):
    """Безопасное получение данных с повторными попытками"""
    for attempt in range(max_retries):
        try:
            return ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit, params={'timeout': 15000})
        except (ccxt.RequestTimeout, ccxt.ExchangeNotAvailable, ccxt.NetworkError) as e:
            if attempt == max_retries - 1:
                raise
            sleep_time = 2 ** attempt
            print(f"⚠️ Retry {attempt + 1} for {symbol} {timeframe} after {sleep_time}s")
            time.sleep(sleep_time)
        except ccxt.RateLimitExceeded:
            time.sleep(5)  # Ждем при лимите запросов

def pick_all_swap_usdt_symbols_with_liquidity(ex: ccxt.Exchange,
                                              min_qv_usdt: float,
                                              min_last_price: float) -> List[str]:
    """Получение символов с обработкой ошибок"""
    try:
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
                
                if qv >= min_qv_usdt and last >= min_last_price:
                    selected.append(sym)
                    
            except Exception:
                continue
                
        return selected
    except Exception as e:
        print(f"❌ Error picking symbols: {e}")
        return []

# ========================= Мониторинг здоровья =========================

def health_check():
    """Проверка здоровья приложения"""
    current_time = time.time()
    
    # Проверка памяти каждые MEMORY_CHECK_INTERVAL циклов
    if health_monitor.cycles_completed % MEMORY_CHECK_INTERVAL == 0:
        health_monitor.memory_usage = get_memory_usage()
        if health_monitor.memory_usage > 500:  # MB
            send_telegram(f"⚠️ Высокое использование памяти: {health_monitor.memory_usage}MB")
            gc.collect()  # Принудительная сборка мусора
    
    # Отчет о состоянии каждые 100 циклов
    if health_monitor.cycles_completed % 100 == 0:
        uptime = health_monitor.get_uptime()
        send_telegram(
            f"📊 Отчет здоровья:\n"
            f"• Uptime: {uptime:.0f} сек\n"
            f"• Циклов: {health_monitor.cycles_completed}\n"
            f"• Ошибок подряд: {health_monitor.consecutive_errors}\n"
            f"• Память: {health_monitor.memory_usage}MB\n"
            f"• Статус: ✅ НОРМА"
        )
    
    # Обслуживание БД каждые 500 циклов
    if health_monitor.cycles_completed % 500 == 0:
        maintenance_db()

def get_memory_usage():
    """Получение использования памяти в MB"""
    try:
        import psutil
        process = psutil.Process(os.getpid())
        return process.memory_info().rss / 1024 / 1024
    except:
        return 0

# ========================= Основная логика (без изменений) =========================

def last_bar_change_pct(ohlcv: list) -> Tuple[float, int, float]:
    if not ohlcv or len(ohlcv) < 2: return 0.0, 0, 0.0
    prev_close = float(ohlcv[-2][4]); last_close = float(ohlcv[-1][4])
    ts = int(ohlcv[-1][0])
    if prev_close == 0: return 0.0, ts, last_close
    return (last_close/prev_close - 1.0)*100.0, ts, last_close

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

def bb(values: List[float], length: int = BB_LEN, mult: float = BB_MULT) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    if len(values) < length: return None, None, None
    window = values[-length:]
    mean = sum(window) / length
    var = sum((x-mean)*(x-mean) for x in window) / length
    std = var ** 0.5
    upper = mean + mult * std
    lower = mean - mult * std
    return mean, upper, lower

def rsi_status_line(r: Optional[float]) -> str:
    if r is None: return "RSI(1m): n/a"
    if r >= RSI_OB:  return f"RSI(1m): <b>{r:.1f}</b> — <b>перегрето</b>"
    if r <= RSI_OS:  return f"RSI(1m): <b>{r:.1f}</b> — <b>перепроданность</b>"
    return f"RSI(1m): <b>{r:.1f}</b> — нейтрально"

def decide_trade_side(direction: str, chg_pct: float, last_close_1m: Optional[float],
                      upper_bb_1m: Optional[float], lower_bb_1m: Optional[float],
                      rsi_1m: Optional[float], pump_thr: float, dump_thr: float) -> Tuple[str, Optional[str]]:
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

def get_unevaluated_spikes(older_than_min: int = 5) -> List[Tuple[str, str, str, int, float]]:
    cutoff_ms = int((now_utc() - timedelta(minutes=older_than_min)).timestamp() * 1000)
    con = sqlite3.connect(STATE_DB, timeout=10.0)
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

def update_spike_outcomes_by_ts(key_symbol: str, timeframe: str, direction: str, candle_ts: int,
                                min_return_60m: float, max_return_60m: float,
                                f5: Optional[float], f15: Optional[float],
                                f30: Optional[float], f60: Optional[float],
                                revert_min: Optional[int]) -> None:
    def _update():
        con = sqlite3.connect(STATE_DB, timeout=10.0)
        cur = con.cursor()
        cur.execute("""
            UPDATE spikes_v2
            SET min_return_60m=?, max_return_60m=?, fwd_5m=?, fwd_15m=?, fwd_30m=?, fwd_60m=?, revert_min=?, evaluated=1
            WHERE key_symbol=? AND timeframe=? AND direction=? AND candle_ts=? AND evaluated=0
        """, (min_return_60m, max_return_60m, f5, f15, f30, f60, revert_min,
              key_symbol, timeframe, direction, int(candle_ts)))
        con.commit()
        con.close()
    db_operation_with_retry(_update)

def recent_symbol_stats(key_symbol: str, timeframe: str, direction: str,
                        days: int = HISTORY_LOOKBACK_DAYS) -> Optional[Dict[str, float]]:
    since_ms = int((now_utc() - timedelta(days=days)).timestamp() * 1000)
    con = sqlite3.connect(STATE_DB, timeout=10.0)
    cur = con.cursor()
    cur.execute("""
        SELECT min_return_60m, max_return_60m, fwd_5m, fwd_15m, fwd_30m, fwd_60m, revert_min
        FROM spikes_v2
        WHERE key_symbol=? AND timeframe=? AND direction=? AND evaluated=1 AND candle_ts>=?
    """, (key_symbol, timeframe, direction, since_ms))
    rows = cur.fetchall()
    con.close()
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

# ========================= ОСНОВНОЙ ЦИКЛ С БЕССМЕРТИЕМ =========================

def process_pending_spikes(fut):
    """Обработка ожидающих спайков"""
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

def process_symbol(fut, sym, timeframe, pump_thr, dump_thr):
    """Обработка одного символа"""
    key_symbol = f"FUT:{sym}"
    try:
        ohlcv = fetch_ohlcv_safe(fut, sym, timeframe=timeframe, limit=200)
        chg, ts_ms, close = last_bar_change_pct(ohlcv)
        if ts_ms == 0: return

        # Контекст 1m
        last1m, rsi1m, up1m, lo1m = one_min_context(fut, sym)

        # Памп
        if chg >= pump_thr:
            insert_spike(key_symbol, timeframe, "pump", ts_ms, close)
            stats = recent_symbol_stats(key_symbol, timeframe, "pump")

            side, reason = decide_trade_side("pump", chg, last1m, up1m, lo1m, rsi1m, pump_thr, dump_thr)
            side_line = f"➡️ Идея: <b>{side}</b>" + (f" ({reason})" if reason else "") if side != "—" else "➡️ Идея: —"

            send_telegram(
                f"🚨 <b>Памп</b> (Futures, {timeframe})\n"
                f"Контракт: <b>{sym}</b>\n"
                f"Рост: <b>{chg:.2f}%</b>\n"
                f"Свеча: {ts_dual(ts_ms)}\n"
                f"{rsi_status_line(rsi1m)}\n"
                f"{side_line}\n\n"
                f"{format_stats_block(stats,'pump')}\n\n"
                f"<i>Не финсовет. Риски на вас.</i>"
            )

        # Дамп
        if chg <= -dump_thr:
            insert_spike(key_symbol, timeframe, "dump", ts_ms, close)
            stats = recent_symbol_stats(key_symbol, timeframe, "dump")

            side, reason = decide_trade_side("dump", chg, last1m, up1m, lo1m, rsi1m, pump_thr, dump_thr)
            side_line = f"➡️ Идея: <b>{side}</b>" + (f" ({reason})" if reason else "") if side != "—" else "➡️ Идея: —"

            send_telegram(
                f"🔻 <b>Дамп</b> (Futures, {timeframe})\n"
                f"Контракт: <b>{sym}</b>\n"
                f"Падение: <b>{chg:.2f}%</b>\n"
                f"Свеча: {ts_dual(ts_ms)}\n"
                f"{rsi_status_line(rsi1m)}\n"
                f"{side_line}\n\n"
                f"{format_stats_block(stats,'dump')}\n\n"
                f"<i>Не финсовет. Риски на вас.</i>"
            )

    except ccxt.RateLimitExceeded:
        time.sleep(2.0)
    except Exception as e:
        print(f"[SCAN] {sym} {timeframe}: {e}")
        time.sleep(0.03)

def main_loop():
    """Бессмертный основной цикл"""
    print("🚀 Запуск БЕССМЕРТНОЙ версии бота...")
    setup_signal_handlers()
    init_db()
    
    send_telegram(
        "🔄 Бот запущен в БЕССМЕРТНОМ режиме!\n"
        f"Пороги: 5m {THRESH_5M_PCT}%/{THRESH_5M_DROP_PCT}% | 15m {THRESH_15M_PCT}%/{THRESH_15M_DROP_PCT}%\n"
        f"Макс ошибок: {MAX_CONSECUTIVE_ERRORS}\n"
        f"Проверка здоровья каждые {MEMORY_CHECK_INTERVAL} циклов"
    )
    
    while True:
        cycle_start = time.time()
        
        try:
            # Сбрасываем счетчик ошибок при успешном цикле
            health_monitor.reset_errors()
            
            # Создаем новое соединение для каждого цикла (предотвращает утечки)
            fut = ex_swap()
            
            # Получаем актуальный список символов
            fut_syms = pick_all_swap_usdt_symbols_with_liquidity(
                fut, MIN_24H_QUOTE_VOLUME_USDT, MIN_LAST_PRICE_USDT
            )
            
            if not fut_syms:
                print("⚠️ Нет символов для мониторинга")
                time.sleep(60)
                continue
            
            # Основная обработка
            process_pending_spikes(fut)
            
            for timeframe, pump_thr, dump_thr in TIMEFRAMES:
                for sym in fut_syms:
                    process_symbol(fut, sym, timeframe, pump_thr, dump_thr)
                    time.sleep(0.01)  # Небольшая пауза между символами
            
            # Увеличиваем счетчик успешных циклов
            health_monitor.increment_cycles()
            
            # Проверка здоровья
            health_check()
            
            # Задержка между циклами
            elapsed = time.time() - cycle_start
            sleep_time = max(1.0, POLL_INTERVAL_SEC - elapsed)
            time.sleep(sleep_time)
            
        except KeyboardInterrupt:
            print("Остановка по Ctrl+C")
            send_telegram("⏹️ Бот остановлен по запросу пользователя")
            break
            
        except Exception as e:
            health_monitor.increment_errors()
            error_msg = f"❌ Ошибка в цикле [{health_monitor.consecutive_errors}]: {str(e)}"
            print(error_msg)
            traceback.print_exc()
            
            # Критическое количество ошибок - перезапуск
            if health_monitor.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                crash_msg = (
                    f"💥 КРИТИЧЕСКАЯ ОШИБКА: {health_monitor.consecutive_errors} ошибок подряд!\n"
                    f"Uptime: {health_monitor.get_uptime():.0f}сек\n"
                    f"Циклов: {health_monitor.cycles_completed}\n"
                    f"Перезапуск через {RESTART_DELAY}сек..."
                )
                send_telegram(crash_msg)
                time.sleep(RESTART_DELAY)
                sys.exit(1)  # Docker перезапустит контейнер
            
            # Уведомление о
