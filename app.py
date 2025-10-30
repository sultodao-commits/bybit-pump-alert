#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bybit Futures Signals Bot - TradingView Logic
УСИЛЕННЫЕ НАСТРОЙКИ
"""

import os
import time
import requests
import ccxt
import numpy as np
from typing import List, Dict, Any, Optional

# ========================= НАСТРОЙКИ =========================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# ========================= УСИЛЕННЫЕ НАСТРОЙКИ =========================

# CORE
RSI_LENGTH = 14
EMA_LENGTH = 50
BB_LENGTH = 20
BB_MULTIPLIER = 1.8

# THRESHOLDS (УСИЛЕНЫ)
RSI_PANIC_THRESHOLD = 32    # Более строгий LONG
RSI_FOMO_THRESHOLD = 68     # Более строгий SHORT
RSI_MODE = "zone-hook"

# FILTERS (УСИЛЕНЫ)
USE_EMA_SIDE_FILTER = True   # ВКЛЮЧИТЬ фильтр по EMA
USE_SLOPE_FILTER = True      # ВКЛЮЧИТЬ фильтр по наклону
MIN_VOLUME_ZSCORE = 0.0      # Объем ДОЛЖЕН быть выше среднего
REQUIRE_RETURN_BB = True     # Строго требовать возврат в BB
REQUIRE_CANDLE_CONFIRM = True
MIN_BODY_PCT = 0.40          # Более строгое тело свечи
USE_HTF_CONFIRM = False

POLL_INTERVAL_SEC = 25
SIGNAL_COOLDOWN_MIN = 15
CHUNK_SIZE = 100

# ========================= ИНДИКАТОРЫ =========================

def calculate_rsi(prices: List[float], period: int = 14) -> float:
    if len(prices) < period + 1:
        return 50.0
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains = [d for d in deltas if d > 0]
    losses = [-d for d in deltas if d < 0]
    if not gains and not losses:
        return 50.0
    avg_gain = sum(gains[-period:]) / period if gains else 0
    avg_loss = sum(losses[-period:]) / period if losses else 0.0001
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return min(max(rsi, 0), 100)

def calculate_ema(prices: List[float], period: int) -> float:
    if len(prices) < period:
        return prices[-1] if prices else 0
    weights = np.exp(np.linspace(-1., 0., period))
    weights /= weights.sum()
    return np.convolve(prices[-period:], weights, mode='valid')[-1]

def calculate_bollinger_bands(prices: List[float], period: int, mult: float) -> tuple:
    if len(prices) < period:
        basis = prices[-1] if prices else 0
        return basis, basis, basis
    basis = np.mean(prices[-period:])
    dev = mult * np.std(prices[-period:])
    upper = basis + dev
    lower = basis - dev
    return basis, upper, lower

def calculate_volume_zscore(volumes: List[float], period: int) -> float:
    if len(volumes) < period:
        return 0.0
    recent_volumes = volumes[-period:]
    mean_vol = np.mean(recent_volumes)
    std_vol = np.std(recent_volumes)
    if std_vol == 0:
        return 0.0
    return (volumes[-1] - mean_vol) / std_vol

# ========================= ЛОГИКА СИГНАЛОВ =========================

def analyze_tv_signals(symbol: str, ohlcv: List) -> Optional[Dict[str, Any]]:
    try:
        if len(ohlcv) < max(RSI_LENGTH, EMA_LENGTH, BB_LENGTH) + 5:
            return None

        closes = [float(c[4]) for c in ohlcv]
        opens = [float(c[1]) for c in ohlcv]
        highs = [float(c[2]) for c in ohlcv]
        lows = [float(c[3]) for c in ohlcv]
        volumes = [float(c[5]) for c in ohlcv]

        current_close = closes[-1]
        current_open = opens[-1]
        current_high = highs[-1]
        current_low = lows[-1]

        # Индикаторы
        rsi = calculate_rsi(closes, RSI_LENGTH)
        ema = calculate_ema(closes, EMA_LENGTH)
        basis, bb_upper, bb_lower = calculate_bollinger_bands(closes, BB_LENGTH, BB_MULTIPLIER)
        volume_zscore = calculate_volume_zscore(volumes, BB_LENGTH)
        
        # Наклон EMA (разница за 3 периода)
        ema_prev = calculate_ema(closes[:-3], EMA_LENGTH) if len(closes) > EMA_LENGTH + 3 else ema
        ema_slope = ema - ema_prev
        slope_up = ema_slope > 0
        slope_down = ema_slope < 0

        # Фильтр объема (УСИЛЕН)
        volume_pass = volume_zscore >= MIN_VOLUME_ZSCORE

        # Фильтр свечи (УСИЛЕН)
        candle_range = max(current_high - current_low, 0.0001)
        body = abs(current_close - current_open)
        body_pct = body / candle_range
        bull_candle_ok = (current_close > current_open) and (body_pct >= MIN_BODY_PCT)
        bear_candle_ok = (current_close < current_open) and (body_pct >= MIN_BODY_PCT)

        # RSI триггеры
        prev_rsi = calculate_rsi(closes[:-1], RSI_LENGTH) if len(closes) > RSI_LENGTH + 1 else 50
        
        long_rsi_cross = (prev_rsi < RSI_PANIC_THRESHOLD) and (rsi > RSI_PANIC_THRESHOLD)
        short_rsi_cross = (prev_rsi > RSI_FOMO_THRESHOLD) and (rsi < RSI_FOMO_THRESHOLD)
        
        long_rsi_hook = (rsi < RSI_PANIC_THRESHOLD) and (rsi > prev_rsi)
        short_rsi_hook = (rsi > RSI_FOMO_THRESHOLD) and (rsi < prev_rsi)
        
        long_rsi_trigger = long_rsi_cross or (RSI_MODE == "zone-hook" and long_rsi_hook)
        short_rsi_trigger = short_rsi_cross or (RSI_MODE == "zone-hook" and short_rsi_hook)

        # BB триггеры (УСИЛЕНЫ - только возврат)
        prev_close = closes[-2] if len(closes) > 1 else current_close
        return_long_bb = (prev_close <= bb_lower) and (current_close > bb_lower)
        return_short_bb = (prev_close >= bb_upper) and (current_close < bb_upper)

        long_bb_trigger = return_long_bb
        short_bb_trigger = return_short_bb

        # Комбинированные триггеры
        long_raw_trigger = long_rsi_trigger or long_bb_trigger
        short_raw_trigger = short_rsi_trigger or short_bb_trigger

        # Фильтры по EMA (УСИЛЕНЫ)
        long_side_ok = (not USE_EMA_SIDE_FILTER) or (current_close >= ema)
        short_side_ok = (not USE_EMA_SIDE_FILTER) or (current_close <= ema)
        
        long_trend_ok = (not USE_SLOPE_FILTER) or slope_up
        short_trend_ok = (not USE_SLOPE_FILTER) or slope_down

        # Подтверждение свечой
        candle_pass_long = REQUIRE_CANDLE_CONFIRM and bull_candle_ok
        candle_pass_short = REQUIRE_CANDLE_CONFIRM and bear_candle_ok

        # Финальные сигналы (ВСЕ фильтры должны пройти)
        long_signal = (long_raw_trigger and candle_pass_long and long_side_ok and 
                      long_trend_ok and volume_pass)
        
        short_signal = (short_raw_trigger and candle_pass_short and short_side_ok and 
                       short_trend_ok and volume_pass)

        if not long_signal and not short_signal:
            return None

        if long_signal:
            signal_type = "LONG"
            confidence = 70 + min(rsi - RSI_PANIC_THRESHOLD, 20)
            trigger_source = "RSI" if long_rsi_trigger else "BB"
        else:
            signal_type = "SHORT"
            confidence = 70 + min(RSI_FOMO_THRESHOLD - rsi, 20)
            trigger_source = "RSI" if short_rsi_trigger else "BB"

        confidence = min(confidence, 95)

        print(f"🎯 {symbol}: {signal_type} | RSI={rsi:.1f} | BB={bb_lower:.4f}-{bb_upper:.4f} | Объем Z={volume_zscore:.2f} | Тело={body_pct:.1%}")

        return {
            "symbol": symbol,
            "type": signal_type,
            "rsi": rsi,
            "ema": ema,
            "bb_upper": bb_upper,
            "bb_lower": bb_lower,
            "volume_zscore": volume_zscore,
            "body_pct": body_pct,
            "trigger": trigger_source,
            "confidence": confidence,
            "timestamp": time.time()
        }

    except Exception as e:
        print(f"Ошибка анализа {symbol}: {e}")
        return None

# ========================= TELEGRAM =========================

def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except:
        pass

def format_signal_message(signal: Dict) -> str:
    if signal["type"] == "LONG":
        emoji = "🟢"
        action = "LONG"
    else:
        emoji = "🔴"
        action = "SHORT"
    
    return (
        f"{emoji} <b>КАЧЕСТВЕННЫЙ {action}</b>\n\n"
        f"<b>Монета:</b> {signal['symbol']}\n"
        f"<b>Уверенность:</b> {signal['confidence']:.1f}%\n\n"
        f"<b>СТРОГИЙ АНАЛИЗ:</b>\n"
        f"• RSI: {signal['rsi']:.1f}\n"
        f"• BB: {signal['bb_lower']:.4f} - {signal['bb_upper']:.4f}\n"
        f"• Объем Z-score: {signal['volume_zscore']:.2f}\n"
        f"• Тело свечи: {signal['body_pct']:.1%}\n"
        f"• Триггер: {signal['trigger']}\n\n"
        f"<i>🎯 Усиленные фильтры - только качественные сигналы</i>"
    )

# ========================= ОСНОВНОЙ ЦИКЛ =========================

def main():
    print("🚀 ЗАПУСК БОТА: УСИЛЕННЫЕ ФИЛЬТРЫ")
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ Укажи TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID!")
        return

    exchange = ccxt.bybit({"enableRateLimit": True})
    recent_signals = {}

    markets = exchange.load_markets()
    symbols = []

    for symbol, market in markets.items():
        try:
            if (market.get("type") == "swap" and market.get("linear") and
                market.get("settle") == "USDT" and "USDT" in symbol and "/" in symbol):
                symbols.append(symbol)
        except:
            continue

    total_symbols = len(symbols)
    print(f"🔍 Найдено монет: {total_symbols}")
    send_telegram(f"🤖 <b>Бот запущен</b>: Усиленные фильтры | {total_symbols} монет")

    signal_count = 0
    chunk_index = 0

    while True:
        try:
            total_chunks = (total_symbols + CHUNK_SIZE - 1) // CHUNK_SIZE
            start_idx = chunk_index * CHUNK_SIZE
            end_idx = min((chunk_index + 1) * CHUNK_SIZE, total_symbols)
            current_chunk = symbols[start_idx:end_idx]
            
            print(f"\n⏱️ Чанк {chunk_index + 1}/{total_chunks} | Сигналов: {signal_count}")

            for symbol in current_chunk:
                try:
                    ohlcv = exchange.fetch_ohlcv(symbol, '15m', limit=50)
                    if not ohlcv or len(ohlcv) < 30:
                        continue

                    signal = analyze_tv_signals(symbol, ohlcv)
                    if not signal:
                        continue

                    now = time.time()
                    if symbol in recent_signals and (now - recent_signals[symbol]) < SIGNAL_COOLDOWN_MIN * 60:
                        continue

                    recent_signals[symbol] = now
                    send_telegram(format_signal_message(signal))
                    signal_count += 1
                    print(f"🔥 КАЧЕСТВЕННЫЙ СИГНАЛ #{signal_count}: {symbol}")

                except Exception as e:
                    continue

            chunk_index = (chunk_index + 1) % total_chunks
            now = time.time()
            recent_signals = {k: v for k, v in recent_signals.items() if now - v < SIGNAL_COOLDOWN_MIN * 60 * 2}

        except Exception as e:
            print(f"💥 Ошибка цикла: {e}")
            time.sleep(10)

        print(f"⏰ Следующий цикл через {POLL_INTERVAL_SEC} сек...")
        time.sleep(POLL_INTERVAL_SEC)

if __name__ == "__main__":
    main()
