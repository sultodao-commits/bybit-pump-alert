#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bybit Futures Signals Bot - REAL-TIME REVERSAL
Только живые развороты, не упавшие монеты
"""

import os
import time
import requests
import ccxt
from typing import List, Dict, Any, Optional

# ========================= СТРОГИЕ РЕАЛЬНЫЕ ФИЛЬТРЫ =========================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# ФИЛЬТРЫ ТОЛЬКО ДЛЯ ЖИВЫХ РАЗВОРОТОВ
MIN_PUMP_STRENGTH = 5          # Памп от 5% 
MAX_PULLBACK_FROM_HIGH = 2.0   # Макс откат 2% от пика (не упавшие!)
MIN_RSI = 75                   # RSI от 75 (реальная перекупленность)
VOLUME_DECREASE = 0.7          # Объем ≤0.7x от пика

# Торговые параметры
TARGET_DUMP = 11               # Цель -11%
STOP_LOSS = 3                  # Стоп-лосс +3%
LEVERAGE = 4                   # Плечо 4x

# Интервалы
POLL_INTERVAL_SEC = 20         # Частое сканирование
SIGNAL_COOLDOWN_MIN = 20       # Кулдаун 20 мин

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

def analyze_live_reversal(symbol: str, ohlcv: List, ticker: Dict) -> Optional[Dict[str, Any]]:
    """Анализ ТОЛЬКО живых разворотов (монеты еще не упали)"""
    try:
        if len(ohlcv) < 15:
            return None
        
        current_candle = ohlcv[-1]
        current_high = float(current_candle[2])
        current_low = float(current_candle[3])
        current_close = float(current_candle[4])
        current_volume = float(current_candle[5])
        current_open = float(current_candle[1])
        
        # 1. Находим абсолютный пик пампа (последние 10 свечей)
        recent_candles = ohlcv[-10:]
        recent_highs = [float(x[2]) for x in recent_candles]
        recent_lows = [float(x[3]) for x in recent_candles]
        
        absolute_high = max(recent_highs)
        absolute_low = min(recent_lows)
        
        # Сила пампа от минимума до максимума
        pump_strength = (absolute_high - absolute_low) / absolute_low * 100
        
        # 2. КРИТИЧЕСКОЕ: проверяем что монета НЕ УПАЛА
        # Текущая цена должна быть близко к пику (не более 2% отката)
        pullback_from_high = (absolute_high - current_close) / absolute_high * 100
        
        # Если откат больше 2% - монета уже упала, пропускаем!
        if pullback_from_high > MAX_PULLBACK_FROM_HIGH:
            return None
        
        # 3. RSI должен быть ВЫСОКИМ (перекупленность)
        closes = [float(x[4]) for x in ohlcv]
        rsi_current = calculate_accurate_rsi(closes)
        
        # Если RSI ниже 75 - нет перекупленности, пропускаем!
        if rsi_current < MIN_RSI:
            return None
        
        # 4. Объем должен СНИЖАТЬСЯ на продолжении роста
        recent_volumes = [float(x[5]) for x in recent_candles]
        volume_peak = max(recent_volumes[:-1]) if len(recent_volumes) > 1 else current_volume
        volume_ratio = current_volume / volume_peak if volume_peak > 0 else 1
        
        # 5. Признаки разворота на текущей свече
        body = abs(current_close - current_open)
        upper_wick = current_high - max(current_open, current_close)
        wick_ratio = upper_wick / body if body > 0 else 0
        
        # Доджи или маленькое тело - неопределенность
        is_doji = body / (current_high - current_low) < 0.1 if (current_high - current_low) > 0 else False
        
        print(f"🔍 {symbol}: памп={pump_strength:.1f}%, откат={pullback_from_high:.1f}%, RSI={rsi_current:.1f}, объем={volume_ratio:.2f}x")
        
        # ОСНОВНЫЕ КРИТЕРИИ ЖИВОГО РАЗВОРОТА:
        conditions = {
            "strong_pump": pump_strength >= MIN_PUMP_STRENGTH,
            "near_high": pullback_from_high <= MAX_PULLBACK_FROM_HIGH,  # Еще у пика
            "overbought": rsi_current >= MIN_RSI,  # Реальная перекупленность
            "volume_decreasing": volume_ratio <= VOLUME_DECREASE,
            "rejection_wick": wick_ratio >= 0.3,  # Сильная верхняя тень
            "not_doji": not is_doji  # Не доджи
        }
        
        conditions_met = sum(conditions.values())
        
        # Нужно минимум 4 из 6 условий, включая ОБЯЗАТЕЛЬНО near_high и overbought
        if (conditions_met >= 4 and 
            conditions["near_high"] and 
            conditions["overbought"] and
            conditions["strong_pump"]):
            
            # Расчет целей - монета еще у пика, цель - падение
            entry_price = current_close
            take_profit = absolute_high * (1 - TARGET_DUMP / 100)
            stop_loss = entry_price * (1 + STOP_LOSS / 100)
            
            confidence = 60 + (conditions_met * 8)
            confidence = min(confidence, 90)
            
            # Определяем стадию разворота
            if pullback_from_high <= 0.5:
                stage = "🟢 ТОЧКА РАЗВОРОТА - у пика"
            elif pullback_from_high <= 1.0:
                stage = "🟡 НАЧАЛО ОТКАТА - небольшой отскок"
            else:
                stage = "🔴 В ПРОГРЕССЕ - откат начался"
            
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
                "timestamp": time.time()
            }
        
        return None
        
    except Exception as e:
        print(f"Ошибка анализа {symbol}: {e}")
        return None

def main():
    print("🎯 ЗАПУСК БОТА ЖИВЫХ РАЗВОРОТОВ 🎯")
    print("⚡ ТОЛЬКО монеты у пиков с RSI 75+!")
    
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ Укажи TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID!")
        return
    
    exchange = ccxt.bybit({"enableRateLimit": True})
    recent_signals = {}
    
    # Загрузка символов с приоритетом волатильным
    markets = exchange.load_markets()
    symbols = []
    
    volatile_keywords = ["PEPE", "FLOKI", "BONK", "SHIB", "DOGE", "MEME", "BOME", "WIF", "POPCAT"]
    
    for symbol, market in markets.items():
        try:
            if (market.get("type") == "swap" and market.get("linear") and 
                market.get("settle") == "USDT" and "USDT" in symbol and "/" in symbol):
                
                # Приоритет волатильным монетам
                if any(keyword in symbol for keyword in volatile_keywords):
                    symbols.insert(0, symbol)
                else:
                    symbols.append(symbol)
                    
                if len(symbols) >= 150:
                    break
        except:
            continue
    
    print(f"🎯 Отслеживаем {len(symbols)} монет на живые развороты")
    
    send_telegram(
        f"🎯 <b>БОТ ЖИВЫХ РАЗВОРОТОВ ЗАПУЩЕН</b>\n\n"
        f"<b>ФИЛЬТРЫ РАЗВОРОТА У ПИКА:</b>\n"
        f"• Памп ≥{MIN_PUMP_STRENGTH}% | Откат ≤{MAX_PULLBACK_FROM_HIGH}%\n"
        f"• RSI ≥{MIN_RSI} | Объем ≤{VOLUME_DECREASE}x\n\n"
        f"<b>ЦЕЛЬ:</b> монеты у максимумов перед падением\n"
        f"<b>ИСКЛЮЧЕНО:</b> уже упавшие монеты с RSI 50\n\n"
        f"<i>⚡ Только качественные сигналы у пиков!</i>"
    )
    
    while True:
        try:
            signals_found = 0
            
            print(f"\n🔄 Сканируем {len(symbols)} монет...")
            
            for symbol in symbols:
                try:
                    ohlcv = exchange.fetch_ohlcv(symbol, '5m', limit=15)
                    ticker = exchange.fetch_ticker(symbol)
                    
                    if not ohlcv or len(ohlcv) < 15:
                        continue
                    
                    signal = analyze_live_reversal(symbol, ohlcv, ticker)
                    
                    if signal:
                        signal_key = symbol
                        current_time = time.time()
                        
                        if signal_key in recent_signals:
                            if (current_time - recent_signals[signal_key]) < SIGNAL_COOLDOWN_MIN * 60:
                                continue
                        
                        recent_signals[signal_key] = current_time
                        send_telegram(format_reversal_message(signal))
                        print(f"🎯 ЖИВОЙ РАЗВОРОТ: {symbol} (RSI: {signal['rsi']:.1f}, от пика: {signal['pullback_from_high']:.1f}%)")
                        signals_found += 1
                    
                    time.sleep(0.02)
                    
                except Exception as e:
                    continue
            
            # Очистка старых сигналов
            current_time = time.time()
            recent_signals = {k: v for k, v in recent_signals.items() 
                            if current_time - v < SIGNAL_COOLDOWN_MIN * 60 * 2}
            
            if signals_found > 0:
                print(f"🎊 Найдено живых разворотов: {signals_found}")
            else:
                print("⏳ Живых разворотов нет - ждем формирования у пиков")
                    
        except Exception as e:
            print(f"💥 Ошибка: {e}")
            time.sleep(10)
        
        print(f"⏰ Следующий цикл через {POLL_INTERVAL_SEC} сек...")
        time.sleep(POLL_INTERVAL_SEC)

def format_reversal_message(signal: Dict) -> str:
    return (
        f"🎯 <b>ЖИВОЙ РАЗВОРОТ У ПИКА</b>\n\n"
        f"<b>Монета:</b> {signal['symbol']}\n"
        f"<b>Стадия:</b> {signal['stage']}\n"
        f"<b>Направление:</b> SHORT 🐻\n"
        f"<b>Уверенность:</b> {signal['confidence']}%\n\n"
        f"<b>КРИТИЧЕСКИЕ ПОКАЗАТЕЛИ:</b>\n"
        f"• Памп: {signal['pump_strength']:.1f}%\n"
        f"• От пика: {signal['pullback_from_high']:.1f}% ⚡\n"
        f"• RSI: {signal['rsi']:.1f} ⚡\n"
        f"• Объем: x{signal['volume_ratio']:.2f}\n"
        f"• Условий: {signal['conditions_met']}\n\n"
        f"<b>ТОРГОВЛЯ:</b>\n"
        f"• Вход: {signal['entry_price']:.6f}\n"
        f"• Цель: {signal['take_profit']:.6f} (-{TARGET_DUMP}%)\n"
        f"• Стоп: {signal['stop_loss']:.6f} (+{STOP_LOSS}%)\n"
        f"• Пик: {signal['pump_high']:.6f}\n"
        f"• Плечо: {signal['leverage']}x\n\n"
        f"<i>⚡ Монета у пика - разворот imminent!</i>"
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
