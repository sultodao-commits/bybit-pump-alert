#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bybit Futures Signals Bot - STRICT 3H PUMP VERSION
Анализирует памп ≥10% за 3 часа, без входов, стопов и отката от пика
"""

import os
import time
import requests
import ccxt
from typing import List, Dict, Any, Optional

# ========================= НАСТРОЙКИ =========================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Фильтры
MIN_PUMP_STRENGTH = 10.0       # Памп от 10% за 3 часа
MIN_RSI = 72                   # RSI ≥72 — перекупленность
VOLUME_DECREASE = 0.75         # Объём ≤0.75x от пика
LEVERAGE = 4                   # Плечо (для справки в сообщении)

# Интервалы
POLL_INTERVAL_SEC = 25         # Проверка каждые 25 сек
SIGNAL_COOLDOWN_MIN = 18       # Кулдаун 18 мин
MAX_SIGNALS_PER_DAY = 20       # Не более 20 сигналов в день

# ========================= ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =========================

def calculate_accurate_rsi(prices: List[float], period: int = 14) -> float:
    """Точный расчёт RSI"""
    if len(prices) < period + 1:
        return 50.0
    
    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = [d for d in deltas if d > 0]
    losses = [-d for d in deltas if d < 0]
    
    if not gains and not losses:
        return 50.0
    
    avg_gain = sum(gains[-period:]) / period if gains else 0
    avg_loss = sum(losses[-period:]) / period if losses else 0.0001
    
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    
    return min(max(rsi, 0), 100)

def analyze_strict_signal(symbol: str, ohlcv: List, ticker: Dict) -> Optional[Dict[str, Any]]:
    """Строгий анализ: памп ≥10% за 3ч, RSI, объём, свеча"""
    try:
        if len(ohlcv) < 40:
            return None
        
        current = ohlcv[-1]
        current_close = float(current[4])
        current_high = float(current[2])
        current_low = float(current[3])
        current_open = float(current[1])
        current_volume = float(current[5])
        
        # === Памп за 3 часа (36 свечей × 5 мин) ===
        last_36 = ohlcv[-36:]
        highs = [float(x[2]) for x in last_36]
        lows = [float(x[3]) for x in last_36]
        pump_strength_3h = (max(highs) - min(lows)) / min(lows) * 100 if min(lows) > 0 else 0.0
        
        if pump_strength_3h < MIN_PUMP_STRENGTH:
            return None
        
        # === RSI ===
        closes = [float(x[4]) for x in ohlcv]
        rsi_value = calculate_accurate_rsi(closes)
        if rsi_value < MIN_RSI:
            return None
        
        # === Объём ===
        volumes = [float(x[5]) for x in ohlcv[-12:]]
        volume_peak = max(volumes[:-2]) if len(volumes) > 2 else current_volume
        volume_ratio = current_volume / volume_peak if volume_peak > 0 else 1
        
        if volume_ratio > VOLUME_DECREASE:
            return None
        
        # === Свеча (тень/тело) ===
        body = abs(current_close - current_open)
        upper_wick = current_high - max(current_open, current_close)
        wick_ratio = upper_wick / body if body > 0 else 0
        is_doji = body / (current_high - current_low) < 0.15 if (current_high - current_low) > 0 else False
        
        # === Фильтры ===
        if wick_ratio < 0.25 or is_doji:
            return None
        
        confidence = 60 + (pump_strength_3h / 2)
        confidence = min(confidence, 90)
        
        print(f"✅ {symbol}: памп_3ч={pump_strength_3h:.1f}%, RSI={rsi_value:.1f}, объём={volume_ratio:.2f}, тень={wick_ratio:.2f}")
        
        return {
            "symbol": symbol,
            "pump_strength_3h": pump_strength_3h,
            "rsi": rsi_value,
            "volume_ratio": volume_ratio,
            "wick_ratio": wick_ratio,
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
    return (
        f"🚀 <b>СТРОГИЙ СИГНАЛ: ПАМП ≥10% / 3ч</b>\n\n"
        f"<b>Монета:</b> {signal['symbol']}\n"
        f"<b>Уверенность:</b> {signal['confidence']:.1f}%\n\n"
        f"<b>АНАЛИЗ:</b>\n"
        f"• Памп (3ч): {signal['pump_strength_3h']:.2f}% ⚡\n"
        f"• RSI: {signal['rsi']:.1f}\n"
        f"• Объём: x{signal['volume_ratio']:.2f}\n"
        f"• Верхняя тень: {signal['wick_ratio']:.2f}\n\n"
        f"<i>🎯 Сильное перекупленное движение — возможен разворот.</i>"
    )

# ========================= ОСНОВНОЙ ЦИКЛ =========================

def main():
    print("🚀 ЗАПУСК БОТА: ПАМП ≥10% ЗА 3 ЧАСА (БЕЗ ОТКАТОВ, БЕЗ ВХОДОВ)")
    
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ Укажи TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID!")
        return
    
    exchange = ccxt.bybit({"enableRateLimit": True})
    recent_signals = {}
    
    markets = exchange.load_markets()
    symbols = []
    volatile_keywords = ["PEPE", "FLOKI", "BONK", "SHIB", "DOGE", "MEME", "BOME", "WIF", "POPCAT", "ORDI", "SATS"]
    
    for symbol, market in markets.items():
        try:
            if (market.get("type") == "swap" and market.get("linear") and 
                market.get("settle") == "USDT" and "USDT" in symbol and "/" in symbol):
                if any(keyword in symbol for keyword in volatile_keywords):
                    symbols.insert(0, symbol)
                else:
                    symbols.append(symbol)
                if len(symbols) >= 160:
                    break
        except:
            continue
    
    print(f"📊 Отслеживаем {len(symbols)} монет")
    send_telegram("🤖 <b>Бот запущен</b>: фильтр ≥10% памп / 3ч, RSI ≥72, объём ≤0.75x.\n<b>Цель:</b> ≤20 сигналов/день.")
    
    daily_signals = 0
    last_reset = time.time()
    
    while True:
        try:
            # Сброс счётчика раз в сутки
            if time.time() - last_reset > 86400:
                daily_signals = 0
                last_reset = time.time()
                print("🔄 Сброс дневного счётчика")
            
            print(f"\n⏱️ Сканирование... | Сегодня: {daily_signals}/{MAX_SIGNALS_PER_DAY}")
            
            for symbol in symbols:
                if daily_signals >= MAX_SIGNALS_PER_DAY:
                    print("🛑 Достигнут лимит 20 сигналов/день")
                    break
                
                try:
                    ohlcv = exchange.fetch_ohlcv(symbol, '5m', limit=40)
                    ticker = exchange.fetch_ticker(symbol)
                    if not ohlcv or len(ohlcv) < 40:
                        continue
                    
                    signal = analyze_strict_signal(symbol, ohlcv, ticker)
                    if not signal:
                        continue
                    
                    key = symbol
                    now = time.time()
                    
                    if key in recent_signals and (now - recent_signals[key]) < SIGNAL_COOLDOWN_MIN * 60:
                        continue
                    
                    recent_signals[key] = now
                    send_telegram(format_signal_message(signal))
                    print(f"🎯 СИГНАЛ #{daily_signals + 1}: {symbol}")
                    daily_signals += 1
                    
                    time.sleep(0.05)
                
                except Exception as e:
                    print(f"Ошибка {symbol}: {e}")
                    continue
            
            # Очистка старых записей
            now = time.time()
            recent_signals = {k: v for k, v in recent_signals.items()
                              if now - v < SIGNAL_COOLDOWN_MIN * 60 * 2}
            
        except Exception as e:
            print(f"💥 Ошибка цикла: {e}")
            time.sleep(10)
        
        print(f"⏰ Следующий цикл через {POLL_INTERVAL_SEC} сек...")
        time.sleep(POLL_INTERVAL_SEC)

if __name__ == "__main__":
    main()
