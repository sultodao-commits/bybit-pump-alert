#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bybit Futures Alerts → Telegram (Pump/Dump, History, Revert, Side Hint)
Оптимизированная версия с батчингом и паузами
"""

import os
import time
import sqlite3
import traceback
from datetime import datetime, timezone, timedelta
from typing import List, Tuple, Optional, Dict

# ========================= ДИАГНОСТИКА =========================
print("=== ДЕБАГ СТАРТ ===")

try:
    import requests
    print("✅ requests импортирован")
except ImportError as e:
    print(f"❌ Ошибка импорта requests: {e}")

try:
    import ccxt
    print("✅ ccxt импортирован") 
except ImportError as e:
    print(f"❌ Ошибка импорта ccxt: {e}")

try:
    from dotenv import load_dotenv
    load_dotenv()
    print("✅ .env загружен")
except Exception as e:
    print(f"❌ Ошибка загрузки .env: {e}")

# Проверяем критичные переменные
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "").strip()
print(f"TELEGRAM_BOT_TOKEN: {'ЕСТЬ' if TELEGRAM_BOT_TOKEN else 'НЕТ'}")
print(f"TELEGRAM_CHAT_ID: {'ЕСТЬ' if TELEGRAM_CHAT_ID else 'НЕТ'}")

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    print("❌ КРИТИЧЕСКАЯ ОШИБКА: Не указаны TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID")
    time.sleep(30)
    exit(1)

print("=== КОНЕЦ ДИАГНОСТИКИ ===")

# ========================= Конфигурация =========================

POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", "120"))  # Увеличили интервал

# Пороги пампов/дампов (% за свечу сигнального ТФ)
THRESH_5M_PCT   = float(os.getenv("THRESH_5M_PCT", "6"))
THRESH_15M_PCT  = float(os.getenv("THRESH_15M_PCT", "12"))
THRESH_5M_DROP_PCT  = float(os.getenv("THRESH_5M_DROP_PCT", "6"))
THRESH_15M_DROP_PCT = float(os.getenv("THRESH_15M_DROP_PCT", "12"))

# Ликвидность
MIN_24H_QUOTE_VOLUME_USDT = float(os.getenv("MIN_24H_QUOTE_VOLUME_USDT", "500000"))
MIN_LAST_PRICE_USDT       = float(os.getenv("MIN_LAST_PRICE_USDT", "0.002"))

# Параметры подсказки-направления
SIDE_HINT_MULT     = float(os.getenv("SIDE_HINT_MULT", "1.8"))
RSI_OB             = float(os.getenv("RSI_OB", "78"))
RSI_OS             = float(os.getenv("RSI_OS", "22"))
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

# ========================= База данных =========================

def init_db() -> None:
    try:
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
        cur.execute("CREATE INDEX IF NOT EXISTS idx_spikes_key_tf_dir ON spikes_v2(key_symbol, timeframe, direction)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_spikes_eval_ts ON spikes_v2(evaluated, candle_ts)")
        con.commit()
        con.close()
        print("✅ База данных инициализирована")
    except Exception as e:
        print(f"❌ Ошибка инициализации БД: {e}")

# ========================= Биржа (Bybit swap) =========================

def ex_swap() -> ccxt.bybit:
    return ccxt.bybit({
        "enableRateLimit": True, 
        "timeout": 30000,
        "options": {"defaultType": "swap"}
    })

def get_all_swap_symbols_optimized(ex: ccxt.Exchange) -> List[str]:
    """Оптимизированное получение всех swap символов USDT"""
    print("🔄 Получаем список символов...")
    try:
        # Загружаем рынки один раз
        markets = ex.load_markets()
        print(f"✅ Загружено рынков: {len(markets)}")
        
        symbols = []
        count = 0
        
        for symbol, market in markets.items():
            try:
                # Быстрая фильтрация по основным параметрам
                if not market.get('swap', False):
                    continue
                if not market.get('linear', False):
                    continue  
                if market.get('settle') != 'USDT':
                    continue
                if market.get('quote') != 'USDT':
                    continue
                    
                # Фильтр leveraged tokens
                base = market.get('base', '')
                if any(tag in base for tag in ["UP","DOWN","3L","3S","4L","4S"]):
                    continue
                    
                symbols.append(symbol)
                count += 1
                
            except Exception as e:
                continue
                
        print(f"✅ Отобрано символов: {len(symbols)}")
        return symbols
        
    except Exception as e:
        print(f"❌ Ошибка получения символов: {e}")
        return []

def fetch_ohlcv_safe(ex: ccxt.Exchange, symbol: str, timeframe: str, limit: int = 5):
    """Безопасное получение OHLCV с обработкой ошибок"""
    try:
        return ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    except ccxt.RateLimitExceeded:
        print(f"⚠️ Rate limit для {symbol}, ждем...")
        time.sleep(5)
        return None
    except ccxt.RequestTimeout:
        print(f"⚠️ Timeout для {symbol}, пропускаем...")
        return None
    except Exception as e:
        print(f"❌ Ошибка OHLCV {symbol} {timeframe}: {e}")
        return None

def last_bar_change_pct(ohlcv: list) -> Tuple[float, int, float]:
    if not ohlcv or len(ohlcv) < 2: 
        return 0.0, 0, 0.0
    try:
        prev_close = float(ohlcv[-2][4])
        last_close = float(ohlcv[-1][4])
        ts = int(ohlcv[-1][0])
        if prev_close == 0: 
            return 0.0, ts, last_close
        return (last_close/prev_close - 1.0)*100.0, ts, last_close
    except Exception as e:
        return 0.0, 0, 0.0

# ========================= Индикаторы (1m) =========================

def rsi(values: List[float], length: int = 14) -> Optional[float]:
    if len(values) <= length: 
        return None
    try:
        gains, losses = [], []
        for i in range(1, len(values)):
            d = values[i] - values[i-1]
            gains.append(max(d, 0.0))
            losses.append(max(-d, 0.0))
        avg_gain = sum(gains[:length]) / length
        avg_loss = sum(losses[:length]) / length
        for i in range(length, len(gains)):
            avg_gain = (avg_gain*(length-1) + gains[i]) / length
            avg_loss = (avg_loss*(length-1) + losses[i]) / length
        if avg_loss == 0: 
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))
    except Exception:
        return None

def one_min_context(ex: ccxt.Exchange, symbol: str):
    try:
        ohlcv = fetch_ohlcv_safe(ex, symbol, timeframe="1m", limit=30)
        if not ohlcv:
            return None, None, None, None
        closes = [float(x[4]) for x in ohlcv]
        last_close = closes[-1] if closes else None
        r = rsi(closes, 14)
        return last_close, r, None, None  # Упростили - убрали BB для скорости
    except Exception as e:
        return None, None, None, None

def rsi_status_line(r: Optional[float]) -> str:
    if r is None: 
        return "RSI(1m): n/a"
    if r >= RSI_OB:  
        return f"RSI(1m): <b>{r:.1f}</b> — <b>🔥 ПЕРЕГРЕТО!</b>"
    if r <= RSI_OS:  
        return f"RSI(1m): <b>{r:.1f}</b> — <b>🧊 ПЕРЕПРОДАНО!</b>"
    return f"RSI(1m): <b>{r:.1f}</b> — нейтрально"

def decide_trade_side(direction: str, chg_pct: float, rsi_1m: Optional[float], pump_thr: float, dump_thr: float) -> Tuple[str, Optional[str]]:
    """
    Упрощенная версия - только RSI для скорости
    """
    if rsi_1m is None:
        return "—", None

    if direction == "pump":
        if chg_pct >= pump_thr * SIDE_HINT_MULT:
            if rsi_1m >= RSI_OB:
                return "SHORT", f"🔥 КРИТИЧЕСКИЙ ПАМП {chg_pct:.1f}% + RSI {rsi_1m:.1f}"
            elif rsi_1m >= 75:
                return "SHORT", f"🚨 Сильный памп {chg_pct:.1f}% + перегрев RSI {rsi_1m:.1f}"
            else:
                return "SHORT", f"⚡ Сильный памп {chg_pct:.1f}%"
    
    elif direction == "dump":
        if chg_pct <= -dump_thr * SIDE_HINT_MULT:
            if rsi_1m <= RSI_OS:
                return "LONG", f"🔥 КРИТИЧЕСКИЙ ДАМП {chg_pct:.1f}% + RSI {rsi_1m:.1f}"
            elif rsi_1m <= 25:
                return "LONG", f"🚨 Сильный дамп {chg_pct:.1f}% + перепродан RSI {rsi_1m:.1f}"
            else:
                return "LONG", f"⚡ Сильный дамп {chg_pct:.1f}%"
    
    return "—", None

def format_signal_message(side: str, reason: Optional[str]) -> str:
    if side == "—" or reason is None:
        return "➡️ Идея: —"
    
    if side == "SHORT":
        emoji = "📉"
        action = "ОТКАТ"
    else:
        emoji = "📈" 
        action = "ОТСКОК"
    
    return f"🎯 СИГНАЛ: {side} {emoji} ({action})\n📊 {reason}"

# ========================= Основной цикл =========================

def main():
    print("🚀 Запуск бота...")
    init_db()
    
    try:
        fut = ex_swap()
        print("✅ Bybit подключен")
    except Exception as e:
        print(f"❌ Ошибка подключения к Bybit: {e}")
        return

    try:
        # Получаем ВСЕ символы
        all_symbols = get_all_swap_symbols_optimized(fut)
        print(f"✅ Всего символов: {len(all_symbols)}")
        
        # Тестовое сообщение в Telegram
        send_telegram(
            f"✅ Бот запущен\n"
            f"Символов: {len(all_symbols)}\n"
            f"Пороги: 5m ≥ {THRESH_5M_PCT}% | 15m ≥ {THRESH_15M_PCT}%\n"
            f"Мониторинг: ВКЛЮЧЕН"
        )
        
    except Exception as e:
        print(f"❌ Ошибка инициализации: {e}")
        all_symbols = []

    print("🔍 Начинаем мониторинг всех символов...")
    
    cycle_count = 0
    while True:
        cycle_count += 1
        cycle_start = time.time()
        
        try:
            print(f"\n=== Цикл #{cycle_count} ===")
            total_signals = 0
            
            # Разбиваем символы на батчи по 50 для избежания rate limits
            batch_size = 50
            symbol_batches = [all_symbols[i:i + batch_size] for i in range(0, len(all_symbols), batch_size)]
            
            for timeframe, pump_thr, dump_thr in TIMEFRAMES:
                signals_found = 0
                print(f"📊 Сканируем {timeframe} ({len(all_symbols)} символов, {len(symbol_batches)} батчей)...")
                
                for batch_num, symbol_batch in enumerate(symbol_batches, 1):
                    print(f"   Батч {batch_num}/{len(symbol_batches)}: {len(symbol_batch)} символов")
                    
                    for symbol in symbol_batch:
                        try:
                            # Получаем данные с паузой
                            ohlcv = fetch_ohlcv_safe(fut, symbol, timeframe=timeframe, limit=3)  # Уменьшили лимит
                            if not ohlcv or len(ohlcv) < 2:
                                continue
                                
                            chg, ts_ms, close = last_bar_change_pct(ohlcv)
                            if abs(chg) < 0.1:  # Игнорируем微小ые изменения
                                continue
                                
                            # Проверяем памп/дамп
                            if chg >= pump_thr:
                                print(f"🚨 ПАМП {symbol} {timeframe}: {chg:.2f}%")
                                signals_found += 1
                                total_signals += 1
                                
                                # Получаем контекст для сигнала
                                last1m, rsi1m, _, _ = one_min_context(fut, symbol)
                                
                                # Формируем сигнал (упрощенный)
                                side, reason = decide_trade_side("pump", chg, rsi1m, pump_thr, dump_thr)
                                
                                if side != "—":
                                    send_telegram(
                                        f"🚨 <b>ПАМП</b> ({timeframe})\n"
                                        f"Контракт: <b>{symbol}</b>\n"
                                        f"Рост: <b>{chg:.2f}%</b> 📈\n"
                                        f"Свеча: {ts_dual(ts_ms)}\n\n"
                                        f"{rsi_status_line(rsi1m)}\n"
                                        f"{format_signal_message(side, reason)}\n\n"
                                        f"<i>Не финсовет. Риски на вас.</i>"
                                    )
                                
                            elif chg <= -dump_thr:
                                print(f"🔻 ДАМП {symbol} {timeframe}: {chg:.2f}%")
                                signals_found += 1
                                total_signals += 1
                                
                                last1m, rsi1m, _, _ = one_min_context(fut, symbol)
                                
                                side, reason = decide_trade_side("dump", chg, rsi1m, pump_thr, dump_thr)
                                
                                if side != "—":
                                    send_telegram(
                                        f"🔻 <b>ДАМП</b> ({timeframe})\n"
                                        f"Контракт: <b>{symbol}</b>\n"
                                        f"Падение: <b>{chg:.2f}%</b> 📉\n"
                                        f"Свеча: {ts_dual(ts_ms)}\n\n"
                                        f"{rsi_status_line(rsi1m)}\n"
                                        f"{format_signal_message(side, reason)}\n\n"
                                        f"<i>Не финсовет. Риски на вас.</i>"
                                    )
                                
                        except Exception as e:
                            print(f"❌ Ошибка сканирования {symbol}: {e}")
                            continue
                    
                    # Пауза между батчами
                    if batch_num < len(symbol_batches):
                        print(f"   💤 Пауза 2 сек между батчами...")
                        time.sleep(2)
                
                print(f"📈 На {timeframe} найдено сигналов: {signals_found}")
            
            print(f"🎯 Всего сигналов в цикле: {total_signals}")
                
        except Exception as e:
            print(f"❌ Ошибка в основном цикле: {e}")
            traceback.print_exc()

        # Ожидание следующего цикла
        elapsed = time.time() - cycle_start
        sleep_time = max(10.0, POLL_INTERVAL_SEC - elapsed)
        print(f"💤 Следующий цикл через {sleep_time:.1f} сек...")
        time.sleep(sleep_time)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("⏹️ Остановка по Ctrl+C")
    except Exception as e:
        print(f"💥 КРИТИЧЕСКАЯ ОШИБКА: {e}")
        traceback.print_exc()
        time.sleep(10)
