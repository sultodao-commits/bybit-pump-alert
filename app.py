#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bybit Futures Signals Bot - ОПТИМАЛЬНЫЙ БАЛАНС
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

# ========================= ОПТИМАЛЬНЫЙ БАЛАНС =========================

# CORE
RSI_LENGTH = 14
BB_LENGTH = 20
BB_MULTIPLIER = 1.8

# THRESHOLDS (БАЛАНС)
RSI_PANIC_THRESHOLD = 40    # Усилено
RSI_FOMO_THRESHOLD = 60     # Усилено

# ФИЛЬТРЫ (МИНИМАЛЬНЫЕ НО ЕСТЬ)
MIN_VOLUME_ZSCORE = -0.8    # Легкий фильтр объема
MIN_BODY_PCT = 0.20         # Легкий фильтр свечи

POLL_INTERVAL_SEC = 25
SIGNAL_COOLDOWN_MIN = 5     # Увеличен кулдаун

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
        if len(ohlcv) < 20:
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
        basis, bb_upper, bb_lower = calculate_bollinger_bands(closes, BB_LENGTH, BB_MULTIPLIER)
        volume_zscore = calculate_volume_zscore(volumes, BB_LENGTH)
        
        # Легкие фильтры
        volume_pass = volume_zscore >= MIN_VOLUME_ZSCORE
        
        candle_range = max(current_high - current_low, 0.0001)
        body = abs(current_close - current_open)
        body_pct = body / candle_range
        bull_candle_ok = (current_close > current_open) and (body_pct >= MIN_BODY_PCT)
        bear_candle_ok = (current_close < current_open) and (body_pct >= MIN_BODY_PCT)

        # Условия
        long_condition = rsi < RSI_PANIC_THRESHOLD
        short_condition = rsi > RSI_FOMO_THRESHOLD
        
        long_bb = current_low <= bb_lower
        short_bb = current_high >= bb_upper

        long_signal = (long_condition or long_bb) and bull_candle_ok and volume_pass
        short_signal = (short_condition or short_bb) and bear_candle_ok and volume_pass

        if not long_signal and not short_signal:
            return None

        if long_signal:
            signal_type = "LONG"
            trigger_source = "RSI" if long_condition else "BB"
        else:
            signal_type = "SHORT"
            trigger_source = "RSI" if short_condition else "BB"

        print(f"🎯 {symbol}: {signal_type} | RSI={rsi:.1f} | Объем Z={volume_zscore:.2f} | Тело={body_pct:.1%}")

        return {
            "symbol": symbol,
            "type": signal_type,
            "rsi": rsi,
            "bb_upper": bb_upper,
            "bb_lower": bb_lower,
            "volume_zscore": volume_zscore,
            "body_pct": body_pct,
            "trigger": trigger_source,
            "confidence": 70,
            "timestamp": time.time()
        }

    except Exception as e:
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
        f"{emoji} <b>{action} СИГНАЛ</b>\n\n"
        f"<b>Монета:</b> {signal['symbol']}\n"
        f"<b>RSI:</b> {signal['rsi']:.1f}\n"
        f"<b>Объем Z-score:</b> {signal['volume_zscore']:.2f}\n"
        f"<b>Тело свечи:</b> {signal['body_pct']:.1%}\n"
        f"<b>Триггер:</b> {signal['trigger']}\n\n"
        f"<i>🎯 Оптимальный баланс</i>"
    )

# ========================= ОСНОВНОЙ ЦИКЛ =========================

def main():
    print("🚀 ЗАПУСК БОТА: ОПТИМАЛЬНЫЙ БАЛАНС")
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
    send_telegram(f"🤖 <b>Бот запущен</b>: Оптимальный баланс | {total_symbols} монет")

    signal_count = 0

    while True:
        try:
            print(f"\n⏱️ Сканирование... | Сигналов: {signal_count}")

            for symbol in symbols:
                try:
                    ohlcv = exchange.fetch_ohlcv(symbol, '15m', limit=20)
                    if not ohlcv or len(ohlcv) < 15:
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
                    print(f"🔥 СИГНАЛ #{signal_count}: {symbol}")

                except Exception as e:
                    continue

            # Очистка старых сигналов
            now = time.time()
            recent_signals = {k: v for k, v in recent_signals.items() if now - v < SIGNAL_COOLDOWN_MIN * 60 * 2}

        except Exception as e:
            print(f"💥 Ошибка цикла: {e}")
            time.sleep(10)

        print(f"⏰ Следующий цикл через {POLL_INTERVAL_SEC} сек...")
        time.sleep(POLL_INTERVAL_SEC)

if __name__ == "__main__":
    main()
