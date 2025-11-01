#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bybit Futures Signals Bot - ТОЛЬКО СИГНАЛЫ
"""

import os
import time
import requests
import ccxt
import numpy as np
from typing import List, Dict, Any, Optional

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

RSI_LENGTH = 14
BB_LENGTH = 20
BB_MULTIPLIER = 1.8
RSI_PANIC_THRESHOLD = 35
RSI_FOMO_THRESHOLD = 65
MIN_VOLUME_ZSCORE = 1.0
MIN_BODY_PCT = 0.25
REQUIRE_BOTH_TRIGGERS = True
POLL_INTERVAL_SEC = 60
SIGNAL_COOLDOWN_MIN = 420

def send_telegram_message(text: str):
    """Отправляем сигнал во все чаты"""
    if not TELEGRAM_BOT_TOKEN:
        return
        
    # Получаем все активные чаты
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if data.get('ok') and data.get('result'):
                chats = set()
                for update in data['result']:
                    if 'message' in update:
                        chat_id = str(update['message']['chat']['id'])
                        chats.add(chat_id)
                
                # Шлем сигнал в каждый чат
                for chat_id in chats:
                    send_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
                    payload = {
                        "chat_id": chat_id,
                        "text": text,
                        "parse_mode": "HTML"
                    }
                    try:
                        requests.post(send_url, json=payload, timeout=5)
                    except:
                        pass
    except:
        pass

def format_signal_message(signal: Dict) -> str:
    if signal["type"] == "LONG":
        arrows = "↗️" * 4
    else:
        arrows = "↘️" * 4
    
    symbol_parts = signal['symbol'].split('/')
    ticker = symbol_parts[0] if symbol_parts else signal['symbol']
    
    return f"{arrows}\n\n<b>{ticker}</b>"

def calculate_rsi(prices: List[float], period: int = 14) -> float:
    if len(prices) < period + 1: return 50.0
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains = [d for d in deltas if d > 0]
    losses = [-d for d in deltas if d < 0]
    if not gains and not losses: return 50.0
    avg_gain = sum(gains[-period:]) / period if gains else 0
    avg_loss = sum(losses[-period:]) / period if losses else 0.0001
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return min(max(rsi, 0), 100)

def calculate_bollinger_bands(prices: List[float], period: int, mult: float) -> tuple:
    if len(prices) < period: return prices[-1], prices[-1], prices[-1]
    basis = np.mean(prices[-period:])
    dev = mult * np.std(prices[-period:])
    return basis, basis + dev, basis - dev

def calculate_volume_zscore(volumes: List[float], period: int) -> float:
    if len(volumes) < period: return 0.0
    recent_volumes = volumes[-period:]
    mean_vol = np.mean(recent_volumes)
    std_vol = np.std(recent_volumes)
    if std_vol == 0: return 0.0
    return (volumes[-1] - mean_vol) / std_vol

def analyze_tv_signals(symbol: str, ohlcv: List) -> Optional[Dict[str, Any]]:
    try:
        if len(ohlcv) < 25: return None

        closes = [float(c[4]) for c in ohlcv]
        opens = [float(c[1]) for c in ohlcv]
        highs = [float(c[2]) for c in ohlcv]
        lows = [float(c[3]) for c in ohlcv]
        volumes = [float(c[5]) for c in ohlcv]

        current_close = closes[-1]
        current_open = opens[-1]
        prev_close = closes[-2] if len(closes) > 1 else current_close

        rsi = calculate_rsi(closes, RSI_LENGTH)
        bb_lower = calculate_bollinger_bands(closes, BB_LENGTH, BB_MULTIPLIER)[2]
        bb_upper = calculate_bollinger_bands(closes, BB_LENGTH, BB_MULTIPLIER)[1]
        volume_zscore = calculate_volume_zscore(volumes, BB_LENGTH)
        
        volume_pass = volume_zscore >= MIN_VOLUME_ZSCORE
        
        candle_range = max(highs[-1] - lows[-1], 0.0001)
        body = abs(current_close - current_open)
        body_pct = body / candle_range
        bull_candle_ok = (current_close > current_open) and (body_pct >= MIN_BODY_PCT)
        bear_candle_ok = (current_close < current_open) and (body_pct >= MIN_BODY_PCT)

        long_rsi = rsi < RSI_PANIC_THRESHOLD
        short_rsi = rsi > RSI_FOMO_THRESHOLD
        long_bb = (prev_close <= bb_lower) and (current_close > bb_lower)
        short_bb = (prev_close >= bb_upper) and (current_close < bb_upper)

        long_signal = long_rsi and long_bb and bull_candle_ok and volume_pass
        short_signal = short_rsi and short_bb and bear_candle_ok and volume_pass

        if not long_signal and not short_signal: return None

        signal_type = "LONG" if long_signal else "SHORT"
        print(f"🎯 {symbol}: {signal_type} | RSI={rsi:.1f}")

        return {
            "symbol": symbol,
            "type": signal_type,
            "timestamp": time.time()
        }

    except:
        return None

def main():
    print("763")
    
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

    print(f"🔍 Монет: {len(symbols)}")
    signal_count = 0

    while True:
        try:
            print(f"Сканирование... | Сигналов: {signal_count}")
            
            for symbol in symbols:
                try:
                    if symbol in recent_signals:
                        if time.time() - recent_signals[symbol] < SIGNAL_COOLDOWN_MIN * 60:
                            continue

                    ohlcv = exchange.fetch_ohlcv(symbol, '15m', limit=25)
                    if not ohlcv or len(ohlcv) < 20: continue

                    signal = analyze_tv_signals(symbol, ohlcv)
                    if not signal: continue

                    recent_signals[symbol] = time.time()
                    signal_count += 1
                    
                    message = format_signal_message(signal)
                    send_telegram_message(message)
                    
                    print(f"🎯 СИГНАЛ #{signal_count}: {symbol}")

                except:
                    continue

        except Exception as e:
            print(f"Ошибка: {e}")
            time.sleep(10)

        time.sleep(POLL_INTERVAL_SEC)

if __name__ == "__main__":
    main()
