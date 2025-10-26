#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bybit Futures Signals Bot - HIGH FREQUENCY REVERSAL
10+ сигналов в день
"""

import os
import time
import requests
import ccxt
from typing import List, Dict, Any, Optional

# ========================= ОПТИМАЛЬНЫЕ НАСТРОЙКИ =========================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# БАЛАНСИРОВАННЫЕ ФИЛЬТРЫ ДЛЯ 10+ СИГНАЛОВ
MIN_PUMP_STRENGTH = 4          # Памп от 4% (было 5)
PRICE_REJECTION = 0.8          # Откат 0.8% от пика (было 1.0)
RSI_OVERBOUGHT = 70            # RSI от 70 (было 72)
VOLUME_DECREASE = 0.85         # Объем ≤0.85x от пика (было 0.8)

# Торговые параметры
TARGET_DUMP = 9               # Цель -9%
STOP_LOSS = 3.5               # Стоп-лосс +3.5%
LEVERAGE = 4                  # Плечо 4x

# Интервалы
POLL_INTERVAL_SEC = 25        # Чаще сканирование (было 40)
SIGNAL_COOLDOWN_MIN = 15      # Меньше кулдаун (было 25)

# ========================= ПРОСТЫЕ ИНДИКАТОРЫ =========================

def calculate_simple_rsi(prices: List[float], period: int = 12) -> float:  # Укороченный период
    """Упрощенный RSI"""
    if len(prices) < period + 1:
        return 50.0
    
    gains = 0
    losses = 0
    
    for i in range(1, period + 1):
        change = prices[-i] - prices[-i-1]
        if change > 0:
            gains += change
        else:
            losses += abs(change)
    
    avg_gain = gains / period
    avg_loss = losses / period if losses > 0 else 0.0001
    
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def analyze_reversal_optimized(symbol: str, ohlcv: List, ticker: Dict) -> Optional[Dict[str, Any]]:
    """Оптимизированный анализ для большего количества сигналов"""
    try:
        if len(ohlcv) < 12:  # Меньше данных для скорости
            return None
        
        # Текущие данные
        current_candle = ohlcv[-1]
        current_high = float(current_candle[2])
        current_low = float(current_candle[3])
        current_close = float(current_candle[4])
        current_volume = float(current_candle[5])
        
        # 1. Анализ пампа (последние 8 свечей)
        recent_highs = [float(x[2]) for x in ohlcv[-8:]]
        recent_lows = [float(x[3]) for x in ohlcv[-8:]]
        
        pump_high = max(recent_highs)
        pump_low = min(recent_lows)
        pump_strength = (pump_high - pump_low) / pump_low * 100
        
        # Слишком слабый памп
        if pump_strength < MIN_PUMP_STRENGTH:
            return None
        
        # 2. Откат от пика
        price_rejection_ratio = (pump_high - current_close) / pump_high * 100
        
        # 3. RSI анализ
        closes = [float(x[4]) for x in ohlcv]
        rsi_current = calculate_simple_rsi(closes)
        
        # 4. Анализ объема
        recent_volumes = [float(x[5]) for x in ohlcv[-8:]]
        max_volume = max(recent_volumes[:-2]) if len(recent_volumes) > 2 else current_volume
        volume_ratio = current_volume / max_volume if max_volume > 0 else 1
        
        # 5. Анализ свечи (верхняя тень)
        current_open = float(current_candle[1])
        body = abs(current_close - current_open)
        upper_wick = current_high - max(current_open, current_close)
        wick_ratio = upper_wick / body if body > 0 else 0
        
        print(f"🔍 {symbol}: памп={pump_strength:.1f}%, откат={price_rejection_ratio:.1f}%, RSI={rsi_current:.1f}, объем={volume_ratio:.2f}x")
        
        # ОСНОВНЫЕ УСЛОВИЯ (2 из 4 должны выполняться + памп)
        conditions_met = 0
        conditions_met += 1 if price_rejection_ratio >= PRICE_REJECTION else 0
        conditions_met += 1 if rsi_current >= RSI_OVERBOUGHT else 0  
        conditions_met += 1 if volume_ratio <= VOLUME_DECREASE else 0
        conditions_met += 1 if wick_ratio >= 0.2 else 0  # Верхняя тень ≥20% (было 25%)
        
        # БОНУС за сильный памп
        bonus_conditions = 0
        if pump_strength > 10:  # Очень сильный памп
            bonus_conditions += 1
        if pump_strength > 15:  # Экстремальный памп
            bonus_conditions += 1
        
        total_conditions = conditions_met + bonus_conditions
        
        # МИНИМУМ 2 основных условия ИЛИ 1 основное + бонусы
        if total_conditions >= 2 and conditions_met >= 1:
            # Расчет целей
            entry_price = current_close
            take_profit = pump_high * (1 - TARGET_DUMP / 100)
            stop_loss = entry_price * (1 + STOP_LOSS / 100)
            
            # Уверенность (базовая + за условия)
            confidence = 55 + (total_conditions * 12)
            confidence = min(confidence, 85)
            
            return {
                "symbol": symbol,
                "direction": "SHORT", 
                "entry_price": entry_price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "pump_high": pump_high,
                "pump_strength": pump_strength,
                "price_rejection": price_rejection_ratio,
                "rsi": rsi_current,
                "volume_ratio": volume_ratio,
                "wick_ratio": wick_ratio,
                "confidence": confidence,
                "leverage": LEVERAGE,
                "conditions_met": total_conditions,
                "timestamp": time.time()
            }
        
        return None
        
    except Exception as e:
        print(f"Ошибка анализа {symbol}: {e}")
        return None

def main():
    print("🎯 ЗАПУСК ВЫСОКОЧАСТОТНОГО БОТА РАЗВОРОТОВ 🎯")
    print("💪 ЦЕЛЬ: 10+ СИГНАЛОВ В ДЕНЬ!")
    
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ Укажи TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID!")
        return
    
    exchange = ccxt.bybit({"enableRateLimit": True})
    recent_signals = {}
    
    # Загрузка БОЛЬШЕ символов
    markets = exchange.load_markets()
    symbols = []
    
    for symbol, market in markets.items():
        try:
            if (market.get("type") == "swap" and market.get("linear") and 
                market.get("settle") == "USDT" and "USDT" in symbol and "/" in symbol):
                # Приоритет мемным и низкокаповым монетам
                if any(x in symbol for x in ["PEPE", "FLOKI", "BONK", "SHIB", "DOGE"]):
                    symbols.insert(0, symbol)  # Мемные в начало
                else:
                    symbols.append(symbol)
                if len(symbols) >= 180:  # БОЛЬШЕ монет (было 120)
                    break
        except:
            continue
    
    print(f"🎯 Отслеживаем {len(symbols)} монет (приоритет мемам)")
    
    send_telegram(
        f"🚀 <b>ВЫСОКОЧАСТОТНЫЙ БОТ РАЗВОРОТОВ ЗАПУЩЕН</b>\n"
        f"<b>Цель:</b> 10+ сигналов в день\n\n"
        f"<b>Фильтры:</b>\n"
        f"• Памп ≥{MIN_PUMP_STRENGTH}% | Откат ≥{PRICE_REJECTION}%\n"  
        f"• RSI ≥{RSI_OVERBOUGHT} | Объем ≤{VOLUME_DECREASE}x\n"
        f"<b>Торговля:</b>\n"
        f"• Цель: -{TARGET_DUMP}% | Стоп: +{STOP_LOSS}%\n"
        f"• Плечо: {LEVERAGE}x | Монет: {len(symbols)}\n\n"
        f"<i>⚡ Оптимизировано для максимального покрытия!</i>"
    )
    
    cycle_count = 0
    daily_signals = 0
    last_reset = time.time()
    
    while True:
        try:
            # Сброс счетчика каждые 24 часа
            if time.time() - last_reset > 86400:
                daily_signals = 0
                last_reset = time.time()
                print("🔄 Сброс дневного счетчика сигналов")
            
            cycle_count += 1
            signals_found = 0
            
            print(f"\n🔄 Цикл #{cycle_count} | Сегодня: {daily_signals}/10+ сигналов")
            
            for symbol in symbols:
                try:
                    ohlcv = exchange.fetch_ohlcv(symbol, '5m', limit=12)  # Меньше данных
                    ticker = exchange.fetch_ticker(symbol)
                    
                    if not ohlcv or len(ohlcv) < 12:
                        continue
                    
                    signal = analyze_reversal_optimized(symbol, ohlcv, ticker)
                    
                    if signal:
                        signal_key = symbol
                        current_time = time.time()
                        
                        # Проверка кулдауна
                        if signal_key in recent_signals:
                            if (current_time - recent_signals[signal_key]) < SIGNAL_COOLDOWN_MIN * 60:
                                continue
                        
                        recent_signals[signal_key] = current_time
                        send_telegram(format_signal_message(signal))
                        print(f"🎯 СИГНАЛ #{daily_signals + 1}: {symbol} (уверенность: {signal['confidence']}%)")
                        signals_found += 1
                        daily_signals += 1
                        
                        # Лимит на очень активные дни
                        if daily_signals >= 25:
                            print("⚠️ Достигнут дневной лимит сигналов")
                            time.sleep(300)  # Пауза 5 минут
                    
                    time.sleep(0.015)  # Минимальная задержка
                    
                except Exception as e:
                    continue
            
            # Очистка старых сигналов
            current_time = time.time()
            recent_signals = {k: v for k, v in recent_signals.items() 
                            if current_time - v < SIGNAL_COOLDOWN_MIN * 60 * 3}
            
            if signals_found > 0:
                print(f"🎊 Найдено сигналов: {signals_found} | Сегодня: {daily_signals}")
            else:
                print("⏳ Сигналов нет в этом цикле")
                    
        except Exception as e:
            print(f"💥 Ошибка: {e}")
            time.sleep(15)
        
        print(f"⏰ Следующий цикл через {POLL_INTERVAL_SEC} сек...")
        time.sleep(POLL_INTERVAL_SEC)

def format_signal_message(signal: Dict) -> str:
    return (
        f"🔁 <b>СИГНАЛ РАЗВОРОТА #{int(signal['timestamp'] % 1000)}</b>\n\n"
        f"<b>Монета:</b> {signal['symbol']}\n"
        f"<b>Направление:</b> SHORT 🐻\n"
        f"<b>Уверенность:</b> {signal['confidence']}%\n\n"
        f"<b>Анализ:</b>\n"
        f"• Памп: {signal['pump_strength']:.1f}%\n"
        f"• Откат: {signal['price_rejection']:.1f}%\n" 
        f"• RSI: {signal['rsi']:.1f}\n"
        f"• Объем: x{signal['volume_ratio']:.2f}\n"
        f"• Условий: {signal['conditions_met']}/4\n\n"
        f"<b>Торговля:</b>\n"
        f"• Вход: {signal['entry_price']:.6f}\n"
        f"• Цель: {signal['take_profit']:.6f} (-{TARGET_DUMP}%)\n"
        f"• Стоп: {signal['stop_loss']:.6f} (+{STOP_LOSS}%)\n"
        f"• Плечо: {signal['leverage']}x\n\n"
        f"<i>⚡ Риск: средний | Потенциал: высокий</i>"
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
