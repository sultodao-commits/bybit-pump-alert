#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bybit Futures Signals Bot - OPTIMAL REVERSAL  
Баланс качества и количества
"""

import os
import time
import requests
import ccxt
from typing import List, Dict, Any, Optional

# ========================= ОПТИМАЛЬНЫЕ НАСТРОЙКИ =========================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# ОПТИМАЛЬНЫЕ ФИЛЬТРЫ ДЛЯ 8-12 СИГНАЛОВ
MIN_PUMP_STRENGTH = 4.5        # Памп от 4.5% (было 5)
MAX_PULLBACK_FROM_HIGH = 2.5   # Макс откат 2.5% от пика (было 2)
MIN_RSI = 72                   # RSI от 72 (было 75)
VOLUME_DECREASE = 0.75         # Объем ≤0.75x от пика (было 0.7)

# Торговые параметры
TARGET_DUMP = 10               # Цель -10%
STOP_LOSS = 3.5                # Стоп-лосс +3.5%
LEVERAGE = 4                   # Плечо 4x

# Интервалы
POLL_INTERVAL_SEC = 25         # Сканирование каждые 25 сек
SIGNAL_COOLDOWN_MIN = 18       # Кулдаун 18 мин

# ========================= УЛУЧШЕННЫЕ ИНДИКАТОРЫ =========================

def calculate_accurate_rsi(prices: List[float], period: int = 14) -> float:
    """Точный расчет RSI"""
    if len(prices) < period + 1:
        return 50.0
    
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    
    gains = [delta for delta in deltas if delta > 0]
    losses = [-delta for delta in deltas if delta < 0]
    
    if not gains and not losses:
        return 50.0
    
    avg_gain = sum(gains[-period:]) / period if gains else 0
    avg_loss = sum(losses[-period:]) / period if losses else 0.0001
    
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    
    return min(max(rsi, 0), 100)

def analyze_optimal_reversal(symbol: str, ohlcv: List, ticker: Dict) -> Optional[Dict[str, Any]]:
    """Оптимальный анализ разворотов - баланс качества/количества"""
    try:
        if len(ohlcv) < 15:
            return None
        
        current_candle = ohlcv[-1]
        current_high = float(current_candle[2])
        current_low = float(current_candle[3])
        current_close = float(current_candle[4])
        current_volume = float(current_candle[5])
        current_open = float(current_candle[1])
        
        # 1. Анализ пампа (последние 10 свечей)
        recent_candles = ohlcv[-10:]
        recent_highs = [float(x[2]) for x in recent_candles]
        recent_lows = [float(x[3]) for x in recent_candles]
        
        absolute_high = max(recent_highs)
        absolute_low = min(recent_lows)
        
        pump_strength = (absolute_high - absolute_low) / absolute_low * 100
        
        # 2. Проверяем что монета НЕ СИЛЬНО УПАЛА (макс откат 2.5%)
        pullback_from_high = (absolute_high - current_close) / absolute_high * 100
        
        if pullback_from_high > MAX_PULLBACK_FROM_HIGH:
            return None
        
        # 3. RSI должен быть ВЫСОКИМ (перекупленность от 72)
        closes = [float(x[4]) for x in ohlcv]
        rsi_current = calculate_accurate_rsi(closes)
        
        if rsi_current < MIN_RSI:
            return None
        
        # 4. Анализ объема
        recent_volumes = [float(x[5]) for x in recent_candles]
        volume_peak = max(recent_volumes[:-2]) if len(recent_volumes) > 2 else current_volume
        volume_ratio = current_volume / volume_peak if volume_peak > 0 else 1
        
        # 5. Признаки разворота
        body = abs(current_close - current_open)
        upper_wick = current_high - max(current_open, current_close)
        wick_ratio = upper_wick / body if body > 0 else 0
        
        # Доджи или маленькое тело
        is_doji = body / (current_high - current_low) < 0.15 if (current_high - current_low) > 0 else False
        
        print(f"🔍 {symbol}: памп={pump_strength:.1f}%, откат={pullback_from_high:.1f}%, RSI={rsi_current:.1f}, объем={volume_ratio:.2f}x")
        
        # ОПТИМАЛЬНЫЕ КРИТЕРИИ:
        conditions = {
            "strong_pump": pump_strength >= MIN_PUMP_STRENGTH,
            "near_high": pullback_from_high <= MAX_PULLBACK_FROM_HIGH,
            "overbought": rsi_current >= MIN_RSI,
            "volume_decreasing": volume_ratio <= VOLUME_DECREASE,
            "rejection_wick": wick_ratio >= 0.25,  # Верхняя тень от 25%
            "not_doji": not is_doji
        }
        
        conditions_met = sum(conditions.values())
        
        # ОПТИМАЛЬНАЯ ЛОГИКА: 3+ условий, включая ОБЯЗАТЕЛЬНЫЕ near_high и overbought
        if (conditions_met >= 3 and 
            conditions["near_high"] and 
            conditions["overbought"]):
            
            # Дополнительные бонусы за сильные сигналы
            bonus_score = 0
            if pump_strength > 8: bonus_score += 1
            if volume_ratio < 0.6: bonus_score += 1
            if wick_ratio > 0.4: bonus_score += 1
            
            # Расчет целей
            entry_price = current_close
            take_profit = absolute_high * (1 - TARGET_DUMP / 100)
            stop_loss = entry_price * (1 + STOP_LOSS / 100)
            
            # Уверенность с бонусами
            confidence = 55 + (conditions_met * 10) + (bonus_score * 5)
            confidence = min(confidence, 85)
            
            # Стадия разворота
            if pullback_from_high <= 1.0:
                stage = "🟢 У ПИКА - разворот imminent"
            elif pullback_from_high <= 1.8:
                stage = "🟡 НАЧАЛО ОТКАТА - хорошая точка"
            else:
                stage = "🔴 ОТКАТ ИДЕТ - еще есть потенциал"
            
            return {
                "symbol": symbol,
                "direction": "SHORT",
                "entry_price": entry_price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "pump_high": absolute_high,
                "pump_strength": pump_strength,
                "pullback_from_high": pullback_from_high,
                "rsi": rsi_current,
                "volume_ratio": volume_ratio,
                "wick_ratio": wick_ratio,
                "confidence": confidence,
                "leverage": LEVERAGE,
                "stage": stage,
                "conditions_met": f"{conditions_met}/6",
                "bonus_score": bonus_score,
                "timestamp": time.time()
            }
        
        return None
        
    except Exception as e:
        print(f"Ошибка анализа {symbol}: {e}")
        return None

def main():
    print("🎯 ЗАПУСК ОПТИМАЛЬНОГО БОТА РАЗВОРОТОВ 🎯")
    print("⚡ ЦЕЛЬ: 8-12 качественных сигналов в день!")
    
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ Укажи TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID!")
        return
    
    exchange = ccxt.bybit({"enableRateLimit": True})
    recent_signals = {}
    
    # Загрузка символов с приоритетом волатильным
    markets = exchange.load_markets()
    symbols = []
    
    volatile_keywords = ["PEPE", "FLOKI", "BONK", "SHIB", "DOGE", "MEME", "BOME", "WIF", "POPCAT", "ORDI", "SATS"]
    
    for symbol, market in markets.items():
        try:
            if (market.get("type") == "swap" and market.get("linear") and 
                market.get("settle") == "USDT" and "USDT" in symbol and "/" in symbol):
                
                # Приоритет волатильным монетам
                if any(keyword in symbol for keyword in volatile_keywords):
                    symbols.insert(0, symbol)
                else:
                    symbols.append(symbol)
                    
                if len(symbols) >= 160:
                    break
        except:
            continue
    
    print(f"🎯 Отслеживаем {len(symbols)} монет")
    
    send_telegram(
        f"⚡ <b>ОПТИМАЛЬНЫЙ БОТ РАЗВОРОТОВ ЗАПУЩЕН</b>\n\n"
        f"<b>ФИЛЬТРЫ (ОПТИМАЛЬНЫЕ):</b>\n"
        f"• Памп ≥{MIN_PUMP_STRENGTH}% | Откат ≤{MAX_PULLBACK_FROM_HIGH}%\n"
        f"• RSI ≥{MIN_RSI} | Объем ≤{VOLUME_DECREASE}x\n\n"
        f"<b>ЦЕЛЬ:</b> 8-12 сигналов в день\n"
        f"<b>КАЧЕСТВО:</b> баланс частоты и точности\n\n"
        f"<i>🎯 Оптимальный баланс качества/количества!</i>"
    )
    
    daily_signals = 0
    last_reset = time.time()
    
    while True:
        try:
            # Сброс счетчика каждые 24 часа
            if time.time() - last_reset > 86400:
                daily_signals = 0
                last_reset = time.time()
                print("🔄 Сброс дневного счетчика сигналов")
            
            signals_found = 0
            
            print(f"\n🔄 Сканируем... | Сегодня: {daily_signals} сигналов")
            
            for symbol in symbols:
                try:
                    ohlcv = exchange.fetch_ohlcv(symbol, '5m', limit=15)
                    ticker = exchange.fetch_ticker(symbol)
                    
                    if not ohlcv or len(ohlcv) < 15:
                        continue
                    
                    signal = analyze_optimal_reversal(symbol, ohlcv, ticker)
                    
                    if signal:
                        signal_key = symbol
                        current_time = time.time()
                        
                        if signal_key in recent_signals:
                            if (current_time - recent_signals[signal_key]) < SIGNAL_COOLDOWN_MIN * 60:
                                continue
                        
                        recent_signals[signal_key] = current_time
                        send_telegram(format_signal_message(signal))
                        print(f"🎯 СИГНАЛ #{daily_signals + 1}: {symbol} (уверенность: {signal['confidence']}%)")
                        signals_found += 1
                        daily_signals += 1
                    
                    time.sleep(0.02)
                    
                except Exception as e:
                    continue
            
            # Очистка старых сигналов
            current_time = time.time()
            recent_signals = {k: v for k, v in recent_signals.items() 
                            if current_time - v < SIGNAL_COOLDOWN_MIN * 60 * 2}
            
            if signals_found > 0:
                print(f"🎊 Найдено сигналов: {signals_found} | Сегодня: {daily_signals}")
            else:
                print("⏳ Сигналов нет в этом цикле")
                    
        except Exception as e:
            print(f"💥 Ошибка: {e}")
            time.sleep(10)
        
        print(f"⏰ Следующий цикл через {POLL_INTERVAL_SEC} сек...")
        time.sleep(POLL_INTERVAL_SEC)

def format_signal_message(signal: Dict) -> str:
    return (
        f"🎯 <b>ОПТИМАЛЬНЫЙ РАЗВОРОТ</b>\n\n"
        f"<b>Монета:</b> {signal['symbol']}\n"
        f"<b>Стадия:</b> {signal['stage']}\n"
        f"<b>Направление:</b> SHORT 🐻\n"
        f"<b>Уверенность:</b> {signal['confidence']}%\n\n"
        f"<b>АНАЛИЗ:</b>\n"
        f"• Памп: {signal['pump_strength']:.1f}%\n"
        f"• От пика: {signal['pullback_from_high']:.1f}% ⚡\n"
        f"• RSI: {signal['rsi']:.1f} ⚡\n"
        f"• Объем: x{signal['volume_ratio']:.2f}\n"
        f"• Условий: {signal['conditions_met']} (+{signal['bonus_score']} бонус)\n\n"
        f"<b>ТОРГОВЛЯ:</b>\n"
        f"• Вход: {signal['entry_price']:.6f}\n"
        f"• Цель: {signal['take_profit']:.6f} (-{TARGET_DUMP}%)\n"
        f"• Стоп: {signal['stop_loss']:.6f} (+{STOP_LOSS}%)\n"
        f"• Плечо: {signal['leverage']}x\n\n"
        f"<i>⚡ Оптимальное соотношение риск/прибыль!</i>"
    )

def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except:
        pass

if __name__ == "__main__":
    main()
