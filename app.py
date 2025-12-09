#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bybit Pump & Dump Scanner - 5min (OPTIMIZED FOR MORE SIGNALS)
"""

import os
import time
import requests
import ccxt
import numpy as np
from typing import List, Dict, Any, Optional

# ========================= НАСТРОЙКИ =========================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

# ========================= НАСТРОЙКИ СКАНЕРА =========================

# PUMP/DUMP DETECTION - СНИЖЕНЫ ПОРОГИ ДЛЯ БОЛЬШЕ СИГНАЛОВ
PRICE_CHANGE_THRESHOLD = 2.5      # Было 5.0 - теперь 2.5% за 5 минут
VOLUME_SPIKE_THRESHOLD = 1.5      # Было 3.0 - теперь 1.5 Z-score
MIN_ABSOLUTE_VOLUME = 30000       # Было 75000 - теперь 30000 USDT

# FILTERS
REQUIRE_VOLUME_CONFIRMATION = True  # Требовать всплеск объема

POLL_INTERVAL_SEC = 20            # Было 30 - сканируем чаще
SIGNAL_COOLDOWN_MIN = 5           # Было 15 - меньше кулдаун

# ========================= ИНДИКАТОРЫ =========================

def calculate_volume_zscore(volumes: List[float], period: int) -> float:
    """Расчет Z-score объема"""
    if len(volumes) < period:
        return 0.0
    recent_volumes = volumes[-period:]
    mean_vol = np.mean(recent_volumes)
    std_vol = np.std(recent_volumes)
    if std_vol == 0:
        return 0.0
    return (volumes[-1] - mean_vol) / std_vol

def calculate_price_change(ohlcv: List) -> float:
    """Расчет изменения цены за последние 2 свечи (10 минут)"""
    if len(ohlcv) < 3:
        return 0.0
    
    # Берем последние 3 свечи для анализа
    current_candle = ohlcv[-1]
    two_candles_ago = ohlcv[-3]
    
    current_close = float(current_candle[4])
    previous_close = float(two_candles_ago[4])
    
    if previous_close == 0:
        return 0.0
    
    return ((current_close - previous_close) / previous_close) * 100

def calculate_rsi(prices: List[float], period: int = 14) -> float:
    """Расчет RSI для фильтрации перекупленности/перепроданности"""
    if len(prices) < period + 1:
        return 50.0
    
    deltas = np.diff(prices)
    seed = deltas[:period]
    up = seed[seed >= 0].sum() / period
    down = -seed[seed < 0].sum() / period
    
    if down == 0:
        return 100.0
    
    rs = up / down
    rsi = 100.0 - (100.0 / (1.0 + rs))
    
    # Сглаживание
    for i in range(period, len(deltas)):
        delta = deltas[i]
        if delta > 0:
            up_val = delta
            down_val = 0.0
        else:
            up_val = 0.0
            down_val = -delta
        
        up = (up * (period - 1) + up_val) / period
        down = (down * (period - 1) + down_val) / period
        
        if down == 0:
            rsi = 100.0
        else:
            rs = up / down
            rsi = 100.0 - (100.0 / (1.0 + rs))
    
    return rsi

# ========================= ЛОГИКА СКАНЕРА PUMP/DUMP =========================

def analyze_pump_dump(symbol: str, ohlcv: List) -> Optional[Dict[str, Any]]:
    try:
        if len(ohlcv) < 30:  # Увеличили для больше данных
            return None

        closes = [float(c[4]) for c in ohlcv]
        volumes = [float(c[5]) for c in ohlcv]
        
        # Текущие значения
        current_volume = volumes[-1]
        current_close = closes[-1]
        
        # Расчет изменения цены за 10 минут (2 свечи)
        price_change = calculate_price_change(ohlcv)
        
        # Расчет RSI для фильтрации
        rsi = calculate_rsi(closes[-30:])  # RSI за последние 30 свечей
        
        # Расчет Z-score объема
        volume_zscore = calculate_volume_zscore(volumes[:-1], 15)  # 15 период
        
        # Проверка абсолютного объема
        volume_pass = current_volume >= MIN_ABSOLUTE_VOLUME
        
        # Определение типа движения
        is_pump = price_change >= PRICE_CHANGE_THRESHOLD
        is_dump = price_change <= -PRICE_CHANGE_THRESHOLD
        
        # ДОПОЛНИТЕЛЬНЫЕ ФИЛЬТРЫ ДЛЯ БОЛЬШЕ СИГНАЛОВ:
        # 1. Исключаем экстремальный RSI (>85 или <15) - там уже перекупленность
        rsi_filter = not (rsi > 85 or rsi < 15)
        
        # 2. Проверяем объем последних 3 свечей
        last_3_volumes = volumes[-3:]
        avg_last_3 = sum(last_3_volumes) / 3
        avg_prev_10 = sum(volumes[-13:-3]) / 10 if len(volumes) >= 13 else avg_last_3
        volume_growth = avg_last_3 / avg_prev_10 if avg_prev_10 > 0 else 1.0
        
        # Комбинированный фильтр объема
        volume_ok = volume_pass and (volume_zscore >= VOLUME_SPIKE_THRESHOLD or volume_growth >= 1.8)
        
        if not ((is_pump or is_dump) and volume_ok and rsi_filter):
            return None
        
        # Определение силы сигнала
        if abs(price_change) >= 5:
            confidence = 85
            strength = "💥 СИЛЬНЫЙ"
        elif abs(price_change) >= 3.5:
            confidence = 75
            strength = "🚨 СРЕДНИЙ"
        else:
            confidence = 65
            strength = "📈 СЛАБЫЙ"
        
        signal_type = "PUMP" if is_pump else "DUMP"
        
        print(f"🎯 {symbol}: {signal_type} | Изменение: {price_change:+.1f}% | Объем Z={volume_zscore:.1f} | RSI={rsi:.1f}")

        return {
            "symbol": symbol,
            "type": signal_type,
            "price_change": price_change,
            "volume_zscore": volume_zscore,
            "volume_usdt": current_volume,
            "current_price": current_close,
            "confidence": confidence,
            "strength": strength,
            "rsi": rsi,
            "volume_growth": volume_growth,
            "timestamp": time.time()
        }

    except Exception as e:
        print(f"❌ Ошибка анализа {symbol}: {e}")
        return None

# ========================= TELEGRAM =========================

def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN:
        return
    
    # Оптимизировано: кэшируем chat_id
    chat_ids = []
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    try:
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            data = response.json()
            if data.get('ok') and data.get('result'):
                chats = set()
                for update in data['result']:
                    if 'message' in update:
                        chat_id = update['message']['chat']['id']
                        chats.add(chat_id)
                
                chat_ids = list(chats)
    except:
        pass
    
    if not chat_ids:
        return
    
    # Отправляем всем чатам
    for chat_id in chat_ids:
        send_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        try:
            requests.post(send_url, json=payload, timeout=3)
            time.sleep(0.1)  # Задержка между отправками
        except:
            pass

def format_signal_message(signal: Dict) -> str:
    symbol_parts = signal['symbol'].split('/')
    ticker = symbol_parts[0] if symbol_parts else signal['symbol']
    
    if signal["type"] == "PUMP":
        emoji = "🚀"
        direction = "ВВЕРХ"
        color = "🟢"
    else:
        emoji = "💥"
        direction = "ВНИЗ"
        color = "🔴"
    
    change = signal['price_change']
    volume_z = signal['volume_zscore']
    rsi_val = signal.get('rsi', 50)
    vol_growth = signal.get('volume_growth', 1.0)
    
    return f"""{emoji} <b>ПАМП/ДАМП СИГНАЛ (5min)</b> {emoji}

{color} <b>{ticker}</b> | {direction}
📊 Изменение: <b>{change:+.1f}%</b> за 10мин
📈 Объем Z-score: <b>{volume_z:.1f}</b>
📊 RSI: <b>{rsi_val:.1f}</b>
📈 Рост объема: <b>{vol_growth:.1f}x</b>
💪 Сила: <b>{signal['strength']}</b>

⏰ Время: {time.strftime('%H:%M:%S')}"""

# ========================= ОСНОВНОЙ ЦИКЛ =========================

def main():
    print("🚀 ЗАПУСК ОПТИМИЗИРОВАННОГО СКАНЕРА - БОЛЬШЕ СИГНАЛОВ!")
    print(f"🔍 Отслеживание движений от {PRICE_CHANGE_THRESHOLD}% за 10 минут")
    print(f"📊 Минимальный объем: {MIN_ABSOLUTE_VOLUME:,} USDT")
    
    if not TELEGRAM_BOT_TOKEN:
        print("⚠️  TELEGRAM_BOT_TOKEN не указан, сигналы не будут отправляться")

    exchange = ccxt.bybit({
        "enableRateLimit": True,
        "options": {
            "defaultType": "swap"  # Оставляем фьючерсный рынок
        }
    })

    recent_signals = {}
    signal_count = 0

    # Получаем все активные USDT пары
    markets = exchange.load_markets()
    symbols = []
    
    for symbol in markets:
        try:
            if (markets[symbol].get('active', False) and 
                'USDT' in symbol and 
                ':USDT' in symbol and
                not symbol.startswith('BTC/USDT') and  # Исключаем BTC
                not symbol.startswith('ETH/USDT') and  # Исключаем ETH
                not symbol.startswith('SOL/USDT')):    # Исключаем SOL
                
                # Проверяем ликвидность
                market_info = markets[symbol]
                if market_info.get('quoteVolume', 0) > 100000:  # Минимальный объем за 24ч
                    symbols.append(symbol)
        except:
            continue

    total_symbols = len(symbols)
    print(f"🔍 Найдено пар для анализа: {total_symbols}")
    
    if TELEGRAM_BOT_TOKEN:
        send_telegram(f"🤖 Оптимизированный сканер запущен | 5min ТФ | Пар: {total_symbols} | Порог: {PRICE_CHANGE_THRESHOLD}%")

    while True:
        try:
            print(f"\n⏱️  Сканирование... | Всего сигналов: {signal_count}")
            current_time = time.time()
            signals_this_cycle = 0
            
            # Случайный порядок для равномерной нагрузки
            import random
            shuffled_symbols = symbols.copy()
            random.shuffle(shuffled_symbols)
            
            for idx, symbol in enumerate(shuffled_symbols):
                try:
                    # Пропускаем если был недавний сигнал
                    if symbol in recent_signals:
                        time_since_last_signal = current_time - recent_signals[symbol]
                        if time_since_last_signal < SIGNAL_COOLDOWN_MIN * 60:
                            continue
                    
                    # Получаем больше свечей для анализа
                    ohlcv = exchange.fetch_ohlcv(symbol, '5m', limit=35)
                    if not ohlcv or len(ohlcv) < 10:
                        continue
                    
                    signal = analyze_pump_dump(symbol, ohlcv)
                    if not signal:
                        continue
                    
                    # Регистрируем сигнал
                    recent_signals[symbol] = current_time
                    signal_count += 1
                    signals_this_cycle += 1
                    
                    # Форматируем и отправляем
                    message = format_signal_message(signal)
                    
                    if TELEGRAM_BOT_TOKEN:
                        send_telegram(message)
                    
                    print(f"🎯 #{signal_count}: {symbol} | {signal['type']} | {signal['price_change']:+.1f}% | RSI: {signal['rsi']:.1f}")
                    
                    # Небольшая пауза между запросами
                    time.sleep(0.05)
                    
                except Exception as e:
                    if "429" in str(e):  # Rate limit
                        time.sleep(2)
                    continue
            
            print(f"📊 Цикл завершен. Сигналов в этом цикле: {signals_this_cycle}")
            
            # Очистка старых сигналов
            current_time = time.time()
            recent_signals = {k: v for k, v in recent_signals.items() 
                            if current_time - v < SIGNAL_COOLDOWN_MIN * 60 * 3}
            
        except Exception as e:
            print(f"💥 Ошибка цикла: {e}")
            time.sleep(5)

        print(f"⏰ Следующий цикл через {POLL_INTERVAL_SEC} сек...")
        time.sleep(POLL_INTERVAL_SEC)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("⏹️ Сканер остановлен")
    except Exception as e:
        print(f"💥 Критическая ошибка: {e}")
        print("🔄 Перезапуск через 5 секунд...")
        time.sleep(5)
        main()
