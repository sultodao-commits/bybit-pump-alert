#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bybit Futures Signals Bot - ULTRA OPTIMIZED
Экстренные исправления для появления сигналов
"""

import os
import time
import traceback
import re
from datetime import datetime
from typing import List, Dict, Any, Optional

import requests
import ccxt

# ========================= СУПЕР-МЯГКИЕ НАСТРОЙКИ =========================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# УЛЬТРА-МЯГКИЕ ФИЛЬТРЫ
PUMP_THRESHOLD = 3           # Памп от 3% (было 5)
RSI_OVERBOUGHT = 60          # RSI от 60 (было 70) - СИЛЬНО СНИЖЕНО
VOLUME_SPIKE_RATIO = 1.2     # Объем от 1.2x (было 1.5)

# Торговые параметры
TARGET_DUMP = 8              # Цель -8% от пика пампа
STOP_LOSS = 5                # Стоп-лосс +5% от входа
LEVERAGE = 5                 # Плечо 5x

# Минимальные фильтры
MAX_MARKET_CAP = 20000000000 # Макс капитализация $20B
MIN_MARKET_CAP = 1000000     # Мин капитализация $1M
MIN_24H_VOLUME = 10000       # Мин объем $10K

# Интервалы
POLL_INTERVAL_SEC = 30       # Интервал сканирования 30 сек
SIGNAL_COOLDOWN_MIN = 10     # Кулдаун на монету 10 мин

# ========================= УПРОЩЕННЫЙ RSI =========================

def calculate_rsi_simple(prices: List[float], period: int = 10) -> float:
    """Упрощенный расчет RSI"""
    if len(prices) < period + 1:
        return 50.0  # Возвращаем нейтральный RSI если данных мало
    
    try:
        gains = 0.0
        losses = 0.0
        
        # Считаем изменения
        for i in range(1, period + 1):
            change = prices[-i] - prices[-i-1]
            if change > 0:
                gains += change
            else:
                losses += abs(change)
        
        # Средние значения
        avg_gain = gains / period
        avg_loss = losses / period if losses > 0 else 0.0001  # Избегаем деления на 0
        
        # Расчет RSI
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        
        return min(max(rsi, 0), 100)  # Ограничиваем диапазон
        
    except Exception as e:
        print(f"Ошибка RSI: {e}")
        return 50.0  # Возвращаем нейтральный RSI при ошибке

# ========================= ОСНОВНОЙ КОД =========================

def analyze_pump_strength(ohlcv: List, volume_data: List) -> Dict[str, Any]:
    """Анализ силы пампа с исправленным RSI"""
    if len(ohlcv) < 3:
        return {"strength": 0, "rsi": 50, "volume_ratio": 1}
    
    try:
        # Анализ цены за последние 2 свечи
        price_changes = []
        for i in range(1, min(3, len(ohlcv))):
            prev_close = float(ohlcv[-1-i][4])
            current_close = float(ohlcv[-1][4])
            if prev_close > 0:
                change = (current_close - prev_close) / prev_close * 100
                price_changes.append(change)
        
        strength = sum(price_changes) / len(price_changes) if price_changes else 0
        
        # ИСПРАВЛЕННЫЙ RSI расчет
        closes = [float(x[4]) for x in ohlcv[-20:]]  # Берем больше данных для RSI
        rsi_val = calculate_rsi_simple(closes, 10)
        
        # Volume spike
        if len(volume_data) >= 10:
            recent_volumes = [float(x[5]) for x in volume_data[-10:]]
            avg_volume = sum(recent_volumes[:-1]) / (len(recent_volumes) - 1)
            current_volume = recent_volumes[-1]
            volume_ratio = current_volume / avg_volume if avg_volume > 0 else 1
        else:
            volume_ratio = 1
        
        return {
            "strength": strength,
            "rsi": rsi_val,
            "volume_ratio": volume_ratio
        }
        
    except Exception as e:
        print(f"Ошибка анализа пампа: {e}")
        return {"strength": 0, "rsi": 50, "volume_ratio": 1}

def analyze_quality_signal(symbol: str, category: str, exchange, ohlcv_5m: List, ohlcv_15m: List, ticker: Dict) -> Optional[Dict[str, Any]]:
    """Анализ сигнала с УЛЬТРА-мягкими фильтрами"""
    try:
        current_price = float(ticker['last'])
        pump_strength = analyze_pump_strength(ohlcv_5m, ohlcv_5m)
        
        print(f"🔍 {symbol}: памп={pump_strength['strength']:.1f}%, RSI={pump_strength['rsi']:.1f}, объем=x{pump_strength['volume_ratio']:.1f}")
        
        # СУПЕР-МЯГКИЕ УСЛОВИЯ
        pump_ok = pump_strength["strength"] >= PUMP_THRESHOLD
        rsi_ok = pump_strength["rsi"] >= RSI_OVERBOUGHT
        volume_ok = pump_strength["volume_ratio"] >= VOLUME_SPIKE_RATIO
        
        # ДОСТАТОЧНО ЛЮБОГО ИЗ УСЛОВИЙ!
        if pump_ok or (pump_ok and volume_ok) or (rsi_ok and volume_ok):
            
            # Находим пик пампа
            recent_highs = [float(x[2]) for x in ohlcv_5m[-5:]]
            pump_high = max(recent_highs) if recent_highs else current_price
            
            entry_price = current_price
            take_profit = pump_high * (1 - TARGET_DUMP / 100)
            stop_loss = entry_price * (1 + STOP_LOSS / 100)
            
            # Расчет уверенности
            confidence = 60  # Базовая уверенность
            if pump_ok:
                confidence += 10
            if rsi_ok:
                confidence += 15
            if volume_ok:
                confidence += 10
            if category == "meme":
                confidence += 10
            
            return {
                "symbol": symbol,
                "category": category,
                "direction": "SHORT",
                "entry_price": entry_price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "pump_high": pump_high,
                "pump_strength": pump_strength["strength"],
                "rsi": pump_strength["rsi"],
                "volume_ratio": pump_strength["volume_ratio"],
                "confidence": confidence,
                "leverage": LEVERAGE,
                "risk_reward": TARGET_DUMP / STOP_LOSS,
                "timestamp": time.time()
            }
        
        return None
        
    except Exception as e:
        print(f"Ошибка анализа {symbol}: {e}")
        return None

def main():
    print("🚀🚀🚀 ЗАПУСК СУПЕР-ОПТИМИЗИРОВАННОГО БОТА 🚀🚀🚀")
    
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ Укажи TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID!")
        return
    
    exchange = ccxt.bybit({"enableRateLimit": True})
    recent_signals = {}
    
    # Простая загрузка символов
    markets = exchange.load_markets()
    symbols = []
    
    for symbol, market in markets.items():
        try:
            if (market.get("type") == "swap" and market.get("linear") and 
                market.get("settle") == "USDT" and "USDT" in symbol):
                symbols.append(symbol)
                if len(symbols) >= 200:  # Ограничиваем для скорости
                    break
        except:
            continue
    
    print(f"🎯 Отслеживаем {len(symbols)} монет")
    
    send_telegram(
        f"🔥 <b>ЭКСТРЕННЫЙ ЗАПУСК - СУПЕР-МЯГКИЕ ФИЛЬТРЫ</b>\n"
        f"<b>Фильтры:</b> Памп ≥{PUMP_THRESHOLD}% | RSI ≥{RSI_OVERBOUGHT} | Объем ≥{VOLUME_SPIKE_RATIO}x\n"
        f"<b>Цель:</b> -{TARGET_DUMP}% | <b>Плечо:</b> {LEVERAGE}x\n"
        f"<b>Монет:</b> {len(symbols)}\n\n"
        f"<i>⚡ СИГНАЛЫ ДОЛЖНЫ ПОЯВИТЬСЯ!</i>"
    )
    
    cycle_count = 0
    
    while True:
        try:
            cycle_count += 1
            signals_found = 0
            
            print(f"\n🔄 Цикл #{cycle_count} - сканируем {len(symbols)} монет...")
            
            for symbol in symbols:
                try:
                    # Быстрая загрузка данных
                    ohlcv_5m = exchange.fetch_ohlcv(symbol, '5m', limit=10)
                    ticker = exchange.fetch_ticker(symbol)
                    
                    if not ohlcv_5m or len(ohlcv_5m) < 3:
                        continue
                    
                    signal = analyze_quality_signal(symbol, "general", exchange, ohlcv_5m, ohlcv_5m, ticker)
                    
                    if signal:
                        signal_key = f"{symbol}_{cycle_count}"
                        if signal_key not in recent_signals:
                            recent_signals[signal_key] = time.time()
                            send_telegram(format_signal_message(signal))
                            print(f"🎉 СИГНАЛ: {symbol} (памп: {signal['pump_strength']:.1f}%, RSI: {signal['rsi']:.1f})")
                            signals_found += 1
                    
                    time.sleep(0.02)  # Минимальная задержка
                    
                except Exception as e:
                    continue
            
            # Очистка старых сигналов
            current_time = time.time()
            recent_signals = {k: v for k, v in recent_signals.items() 
                            if current_time - v < SIGNAL_COOLDOWN_MIN * 60}
            
            if signals_found > 0:
                print(f"🎊 НАЙДЕНО СИГНАЛОВ: {signals_found}")
            else:
                print("😞 Сигналов не найдено")
                    
        except Exception as e:
            print(f"💥 Ошибка: {e}")
            time.sleep(10)
        
        print(f"⏰ Следующий цикл через {POLL_INTERVAL_SEC} сек...")
        time.sleep(POLL_INTERVAL_SEC)

def format_signal_message(signal: Dict) -> str:
    return (
        f"🎯 <b>СИГНАЛ НАЙДЕН!</b>\n\n"
        f"<b>Монета:</b> {signal['symbol']}\n"
        f"<b>Направление:</b> SHORT 🐻\n"
        f"<b>Памп:</b> {signal['pump_strength']:.1f}%\n"
        f"<b>RSI:</b> {signal['rsi']:.1f}\n"
        f"<b>Объем:</b> x{signal['volume_ratio']:.1f}\n"
        f"<b>Вход:</b> {signal['entry_price']:.6f}\n"
        f"<b>Цель:</b> {signal['take_profit']:.6f}\n"
        f"<b>Уверенность:</b> {signal['confidence']:.0f}%\n\n"
        f"<i>⚡ Мягкие фильтры - проверяй риск!</i>"
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
