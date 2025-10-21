#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bybit Futures Signals Bot - Pump Reversion Strategy
Сигналы для мемкоинов, низкокап и среднекап альтов
"""

import os
import time
import sqlite3
import traceback
import re
from datetime import datetime, timezone, timedelta
from typing import List, Tuple, Optional, Dict, Any

import requests
import ccxt
from dotenv import load_dotenv

# ========================= Конфигурация =========================

load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Параметры стратегии
PUMP_THRESHOLD = float(os.getenv("PUMP_THRESHOLD", "10"))
RSI_OVERBOUGHT = float(os.getenv("RSI_OVERBOUGHT", "80"))
MIN_RETRACEMENT = float(os.getenv("MIN_RETRACEMENT", "25"))
MAX_RETRACEMENT = float(os.getenv("MAX_RETRACEMENT", "60"))
VOLUME_SPIKE_RATIO = float(os.getenv("VOLUME_SPIKE_RATIO", "2.5"))

# Рекомендуемые параметры рисков
RECOMMENDED_LEVERAGE = int(os.getenv("RECOMMENDED_LEVERAGE", "8"))
RECOMMENDED_STOP_LOSS = float(os.getenv("RECOMMENDED_STOP_LOSS", "2.5"))
RECOMMENDED_TAKE_PROFIT = float(os.getenv("RECOMMENDED_TAKE_PROFIT", "5"))

# Фильтры капитализации
MAX_MARKET_CAP = float(os.getenv("MAX_MARKET_CAP", "5000000000"))
MIN_MARKET_CAP = float(os.getenv("MIN_MARKET_CAP", "10000000"))

POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", "30"))

# ========================= Категории монет =========================

MEME_KEYWORDS = [
    'DOGE', 'SHIB', 'PEPE', 'FLOKI', 'BONK', 'MEME', 'WIF', 'BOME', 'BABYDOGE',
    'ELON', 'DOG', 'CAT', 'HAM', 'TURBO', 'AIDOGE', 'AISHIB', 'PENGU', 'MOCHI',
    'WOJAK', 'KABOSU', 'KISHU', 'SAMO', 'SNEK', 'POPCAT', 'LILY', 'MOG', 'TOSHI',
    'HIPO', 'CHAD', 'GROK', 'LADYS', 'VOY', 'COQ', 'KERMIT', 'SPX', 'TRUMP',
    'BODEN', 'TREMP', 'SC', 'SMURFCAT', 'ANDY', 'WEN', 'MYRO', 'WU', 'MICHI'
]

PROMISING_LOWCAPS = [
    'AI', 'ARB', 'OP', 'APT', 'SUI', 'SEI', 'TIA', 'INJ', 'RNDR', 'FET', 
    'AGIX', 'OCEAN', 'NMR', 'LINK', 'BAND', 'DIA', 'TRB', 'UMA', 'API3',
    'GRT', 'LPT', 'LQTY', 'CRV', 'FXS', 'BAL', 'SNX', 'SUSHI', 'CAKE',
    'DYDX', 'PERP', 'GMX', 'GNS', 'VELA', 'RPL', 'LDO', 'FXS', 'FIS',
    'AAVE', 'COMP', 'MKR', 'YFI', 'ALPHA', 'ENS', 'RARE', 'SUPER', 'TVK',
    'SAND', 'MANA', 'GALA', 'ENJ', 'AXS', 'SLP', 'ILV', 'YGG', 'MC',
    'MATIC', 'AVAX', 'FTM', 'ONE', 'ALGO', 'NEAR', 'ATOM', 'OSMO', 'JUNO'
]

LARGE_CAP_EXCLUSIONS = [
    'BTC', 'ETH', 'BNB', 'XRP', 'ADA', 'SOL', 'DOT', 'LTC', 'BCH', 'XLM',
    'LINK', 'ATOM', 'XMR', 'ETC', 'XTZ', 'EOS', 'AAVE', 'ALGO', 'AVAX',
    'AXS', 'BAT', 'COMP', 'DASH', 'ENJ', 'FIL', 'GRT', 'ICP', 'KSM', 'MANA'
]

# ========================= Классификация монет =========================

def classify_symbol(symbol: str, market_data: Dict) -> str:
    """Классификация монеты по категориям"""
    base_symbol = symbol.split('/')[0] if '/' in symbol else symbol
    
    if base_symbol in LARGE_CAP_EXCLUSIONS:
        return "largecap"
    
    if is_meme_coin(symbol):
        return "meme"
    
    if base_symbol in PROMISING_LOWCAPS:
        return "promising_lowcap"
    
    return "other_alt"

def is_meme_coin(symbol: str) -> bool:
    """Проверка на мемкоин"""
    base_symbol = symbol.split('/')[0] if '/' in symbol else symbol
    
    for keyword in MEME_KEYWORDS:
        if keyword in base_symbol.upper():
            return True
    
    meme_patterns = [
        re.compile(r'.*DOGE.*', re.IGNORECASE),
        re.compile(r'.*SHIB.*', re.IGNORECASE),
        re.compile(r'.*PEPE.*', re.IGNORECASE),
        re.compile(r'.*FLOKI.*', re.IGNORECASE),
        re.compile(r'.*BONK.*', re.IGNORECASE),
        re.compile(r'.*MEME.*', re.IGNORECASE),
    ]
    
    for pattern in meme_patterns:
        if pattern.match(base_symbol):
            return True
    
    return False

def get_symbols_by_category(exchange) -> Dict[str, List[str]]:
    """Получение символов по категориям"""
    markets = exchange.load_markets()
    tickers = exchange.fetch_tickers()
    
    categorized = {
        "meme": [],
        "promising_lowcap": [], 
        "other_alt": [],
        "largecap": []
    }
    
    for symbol, market in markets.items():
        try:
            if (market.get("type") != "swap" or not market.get("swap") or 
                not market.get("linear") or market.get("settle") != "USDT"):
                continue
            
            ticker = tickers.get(symbol, {})
            quote_volume = float(ticker.get('quoteVolume', 0))
            last_price = float(ticker.get('last', 0))
            
            if quote_volume < 100000 or last_price < 0.0001:
                continue
            
            category = classify_symbol(symbol, market)
            
            estimated_mcap = get_market_cap_estimate(ticker)
            if estimated_mcap and estimated_mcap > MAX_MARKET_CAP:
                continue
            if estimated_mcap and estimated_mcap < MIN_MARKET_CAP and category != "meme":
                continue
            
            categorized[category].append(symbol)
            
        except Exception:
            continue
    
    return categorized

def get_market_cap_estimate(ticker_data: Dict) -> Optional[float]:
    """Грубая оценка капитализации"""
    try:
        last_price = float(ticker_data.get('last', 0))
        base_volume = float(ticker_data.get('baseVolume', 0))
        
        if last_price > 0 and base_volume > 0:
            return base_volume * last_price * 3
    except Exception:
        pass
    return None

# ========================= Анализ сигналов =========================

def analyze_pump_strength(ohlcv: List, volume_data: List) -> Dict[str, Any]:
    """Анализ силы пампа"""
    if len(ohlcv) < 10:
        return {"strength": 0, "volume_spike": False, "rsi": 50, "volume_ratio": 1}
    
    price_changes = []
    for i in range(1, 4):
        if len(ohlcv) > i:
            change = (ohlcv[-1][4] - ohlcv[-1-i][4]) / ohlcv[-1-i][4] * 100
            price_changes.append(change)
    
    closes = [x[4] for x in ohlcv[-14:]]
    rsi_val = calculate_rsi(closes)
    
    avg_volume = sum([x[5] for x in volume_data[-20:-1]]) / 19 if len(volume_data) >= 20 else volume_data[-1][5]
    volume_ratio = volume_data[-1][5] / avg_volume if avg_volume > 0 else 1
    
    strength = sum(price_changes) / len(price_changes) if price_changes else 0
    volume_spike = volume_ratio > VOLUME_SPIKE_RATIO
    
    return {
        "strength": strength,
        "volume_spike": volume_spike,
        "rsi": rsi_val,
        "volume_ratio": volume_ratio
    }

def calculate_rsi(prices: List[float], period: int = 14) -> float:
    """Расчет RSI"""
    if len(prices) < period + 1:
        return 50
    
    gains = []
    losses = []
    
    for i in range(1, len(prices)):
        change = prices[i] - prices[i-1]
        if change > 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))
    
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    
    if avg_loss == 0:
        return 100
    
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    
    return rsi

def calculate_fibonacci_levels(low: float, high: float) -> Dict[str, float]:
    """Расчет уровней Фибоначчи"""
    diff = high - low
    return {
        "23.6%": high - 0.236 * diff,
        "38.2%": high - 0.382 * diff,
        "50.0%": high - 0.5 * diff,
        "61.8%": high - 0.618 * diff,
        "78.6%": high - 0.786 * diff
    }

def analyze_quality_signal(symbol: str, category: str, exchange, ohlcv_5m: List, ohlcv_15m: List, ticker: Dict) -> Optional[Dict[str, Any]]:
    """Анализ качественного сигнала с учетом категории"""
    try:
        current_price = ticker['last']
        
        pump_strength = analyze_pump_strength(ohlcv_5m, ohlcv_5m)
        
        if category == "meme":
            min_pump = PUMP_THRESHOLD
            min_rsi = RSI_OVERBOUGHT
            min_volume = VOLUME_SPIKE_RATIO
        elif category == "promising_lowcap":
            min_pump = PUMP_THRESHOLD * 0.8
            min_rsi = RSI_OVERBOUGHT - 5
            min_volume = VOLUME_SPIKE_RATIO * 0.8
        else:
            min_pump = PUMP_THRESHOLD
            min_rsi = RSI_OVERBOUGHT
            min_volume = VOLUME_SPIKE_RATIO
        
        if not (pump_strength["strength"] >= min_pump and 
                pump_strength["rsi"] >= min_rsi and
                pump_strength["volume_ratio"] >= min_volume):
            return None
        
        pump_high = max([x[2] for x in ohlcv_5m[-6:]])
        pump_low = min([x[3] for x in ohlcv_5m[-12:-6]])
        
        total_move = pump_high - pump_low
        if total_move <= 0:
            return None
            
        current_retrace = (pump_high - current_price) / total_move * 100
        
        if not (MIN_RETRACEMENT <= current_retrace <= MAX_RETRACEMENT):
            return None
        
        confidence = calculate_confidence(ohlcv_15m, current_retrace, pump_strength, category)
        
        if category == "promising_lowcap":
            confidence += 5
        
        entry_price = current_price
        stop_loss = entry_price * (1 + RECOMMENDED_STOP_LOSS / 100)
        take_profit = entry_price * (1 - RECOMMENDED_TAKE_PROFIT / 100)
        
        fib_levels = calculate_fibonacci_levels(pump_low, pump_high)
        
        return {
            "symbol": symbol,
            "category": category,
            "direction": "SHORT",
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "pump_high": pump_high,
            "pump_low": pump_low,
            "current_retrace": current_retrace,
            "pump_strength": pump_strength["strength"],
            "rsi": pump_strength["rsi"],
            "volume_ratio": pump_strength["volume_ratio"],
            "confidence": confidence,
            "leverage": RECOMMENDED_LEVERAGE,
            "risk_reward": RECOMMENDED_TAKE_PROFIT / RECOMMENDED_STOP_LOSS,
            "fib_levels": fib_levels,
            "timestamp": time.time()
        }
        
    except Exception as e:
        print(f"Ошибка анализа сигнала {symbol}: {e}")
        return None

def calculate_confidence(ohlcv_15m: List, retracement: float, pump_strength: Dict, category: str) -> float:
    """Расчет уверенности в сигнале с учетом категории"""
    confidence = 50
    
    if 30 <= retracement <= 40:
        confidence += 20
    elif 25 <= retracement <= 50:
        confidence += 10
    
    if pump_strength["rsi"] >= 85:
        confidence += 15
    elif pump_strength["rsi"] >= 80:
        confidence += 10
    
    if pump_strength["volume_ratio"] >= 4:
        confidence += 15
    elif pump_strength["volume_ratio"] >= 2.5:
        confidence += 10
    
    if category == "promising_lowcap":
        confidence += 5
    elif category == "meme":
        confidence -= 2
    
    return min(confidence, 95)

# ========================= Форматирование сообщений =========================

def format_signal_message(signal: Dict) -> str:
    """Форматирование торгового сигнала с указанием категории"""
    symbol = signal["symbol"]
    category = signal["category"]
    entry = signal["entry_price"]
    stop = signal["stop_loss"]
    take = signal["take_profit"]
    
    category_emojis = {
        "meme": "🐶",
        "promising_lowcap": "🚀", 
        "other_alt": "💎"
    }
    
    category_name = {
        "meme": "Мемкоин",
        "promising_lowcap": "Перспективный низкокап",
        "other_alt": "Альткоин"
    }
    
    emoji = category_emojis.get(category, "📊")
    cat_name = category_name.get(category, "Альткоин")
    
    # ИСПРАВЛЕННАЯ F-СТРОКА - убраны лишние скобки
    message = (
        f"🎯 СИГНАЛ ДЛЯ ВХОДА 🎯\n"
        f"{emoji} Категория: {cat_name}\n\n"
        f"Монета: {symbol}\n"
        f"Направление: SHORT 🐻\n"
        f"Тип: Откат после пампа\n\n"
        f"📊 СТАТИСТИКА ПАМПА:\n"
        f"• Сила пампа: {signal['pump_strength']:.1f}%\n"
        f"• RSI: {signal['rsi']:.1f} (перекупленность)\n"
        f"• Объем: x{signal['volume_ratio']:.1f} от среднего\n"
        f"• Откат: {signal['current_retrace']:.1f}% от движения\n\n"
        f"💎 ПАРАМЕТРЫ ВХОДА:\n"
        f"• Цена входа: {entry:.6f}\n"
        f"• Стоп-лосс: {stop:.6f} (+{RECOMMENDED_STOP_LOSS}%)\n"
        f"• Тейк-профит: {take:.6f} (-{RECOMMENDED_TAKE_PROFIT}%)\n"
        f"• Плечо: {signal['leverage']}x\n"
        f"• Risk/Reward: 1:{signal['risk_reward']:.1f}\n\n"
        f"📈 УРОВНИ ФИБОНАЧЧИ:\n"
    )
    
    for level, price in signal["fib_levels"].items():
        message += f"• {level}: {price:.6f}\n"
    
    message += f"\n"
    message += f"⚡ УВЕРЕННОСТЬ: {signal['confidence']:.0f}%\n"
    message += f"🕒 Время: {datetime.now().strftime('%H:%M:%S')}\n\n"
    
    if category == "meme":
        message += (
            f"⚠️ ВНИМАНИЕ: Мемкоины - высокорисковые активы!\n"
            f"Крайняя волатильность! Используйте строгое управление капиталом!"
        )
    elif category == "promising_lowcap":
        message += (
            f"💡 Перспективный проект с хорошими фундаменталами.\n"
            f"Более предсказуемая волатильность."
        )
    else:
        message += (
            f"📊 Стандартный альткоин.\n"
            f"Средний уровень риска."
        )
    
    return message

# ========================= Основной цикл =========================

def send_telegram(text: str) -> None:
    """Отправка сообщения в Telegram"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID, 
        "text": text, 
        "parse_mode": "HTML", 
        "disable_web_page_preview": True
    }
    try:
        requests.post(url, json=payload, timeout=15)
    except Exception as e:
        print(f"Ошибка отправки в Telegram: {e}")

def check_existing_signals(symbol: str, new_signal: Dict, recent_signals: Dict, cooldown_min: int = 60) -> bool:
    """Проверка на дублирование сигналов"""
    if symbol in recent_signals:
        last_signal_time = recent_signals[symbol]
        if time.time() - last_signal_time < cooldown_min * 60:
            return False
    
    recent_signals[symbol] = time.time()
    return True

def main():
    print("Запуск сигнального бота для всех категорий альтов...")
    
    exchange = ccxt.bybit({"enableRateLimit": True})
    recent_signals = {}
    signal_cooldown = 60
    
    categorized_symbols = get_symbols_by_category(exchange)
    
    total_symbols = sum(len(symbols) for symbols in categorized_symbols.values())
    
    send_telegram(
        f"✅ УНИВЕРСАЛЬНЫЙ СИГНАЛЬНЫЙ БОТ ЗАПУЩЕН\n"
        f"Охват категорий:\n"
        f"• 🐶 Мемкоины: {len(categorized_symbols['meme'])}\n"
        f"• 🚀 Перспективные низкокапы: {len(categorized_symbols['promising_lowcap'])}\n"
        f"• 💎 Другие альты: {len(categorized_symbols['other_alt'])}\n"
        f"• 📊 Крупные капы: {len(categorized_symbols['largecap'])} (исключены)\n\n"
        f"Всего отслеживаем: {total_symbols} монет\n\n"
        f"Ожидаем качественные сигналы по всем категориям!"
    )
    
    print(f"Найдено монет:")
    print(f"- Мемкоины: {len(categorized_symbols['meme'])}")
    print(f"- Перспективные низкокапы: {len(categorized_symbols['promising_lowcap'])}")
    print(f"- Другие альты: {len(categorized_symbols['other_alt'])}")
    
    while True:
        try:
            quality_signals = []
            
            for category in ["meme", "promising_lowcap", "other_alt"]:
                symbols = categorized_symbols[category]
                
                for symbol in symbols:
                    try:
                        ohlcv_5m = exchange.fetch_ohlcv(symbol, '5m', limit=50)
                        ohlcv_15m = exchange.fetch_ohlcv(symbol, '15m', limit=20)
                        ticker = exchange.fetch_ticker(symbol)
                        
                        if not ohlcv_5m or not ohlcv_15m:
                            continue
                        
                        signal = analyze_quality_signal(symbol, category, exchange, ohlcv_5m, ohlcv_15m, ticker)
                        
                        if signal and signal["confidence"] >= 60:
                            if check_existing_signals(symbol, signal, recent_signals, signal_cooldown):
                                quality_signals.append(signal)
                        
                        time.sleep(0.05)
                        
                    except Exception as e:
                        continue
            
            quality_signals.sort(key=lambda x: x["confidence"], reverse=True)
            
            for signal in quality_signals[:5]:
                message = format_signal_message(signal)
                send_telegram(message)
                print(f"Отправлен сигнал {signal['category']} - {signal['symbol']} (уверенность: {signal['confidence']:.0f}%)")
                time.sleep(2)
                    
        except Exception as e:
            print(f"Ошибка основного цикла: {e}")
            time.sleep(10)
        
        print(f"Цикл завершен. Отслеживаем {total_symbols} монет...")
        time.sleep(POLL_INTERVAL_SEC)

if __name__ == "__main__":
    main()
