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
MAX_MARKET_CAP = float(os.getenv("MAX_MARKET_CAP", "5000000000"))  # 5B max
MIN_MARKET_CAP = float(os.getenv("MIN_MARKET_CAP", "10000000"))    # 10M min

POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", "30"))

# ========================= Категории монет =========================

# Расширенный список мемкоинов
MEME_KEYWORDS = [
    'DOGE', 'SHIB', 'PEPE', 'FLOKI', 'BONK', 'MEME', 'WIF', 'BOME', 'BABYDOGE',
    'ELON', 'DOG', 'CAT', 'HAM', 'TURBO', 'AIDOGE', 'AISHIB', 'PENGU', 'MOCHI',
    'WOJAK', 'KABOSU', 'KISHU', 'SAMO', 'SNEK', 'POPCAT', 'LILY', 'MOG', 'TOSHI',
    'HIPO', 'CHAD', 'GROK', 'LADYS', 'VOY', 'COQ', 'KERMIT', 'SPX', 'TRUMP',
    'BODEN', 'TREMP', 'SC', 'SMURFCAT', 'ANDY', 'WEN', 'MYRO', 'WU', 'MICHI',
    'NUB', 'DAVE', 'PONKE', 'MON', 'PUDGY', 'POWELL', 'PENG', 'SATOSHI', 'VITALIK',
    'KEVIN', 'OSAK', 'BRETT', 'ZYN', 'TAMA', 'NEIRO', 'NOOT', 'SPUG', 'PIRB',
    'MOUTAI', 'MOG', 'MILADY', 'STAN', 'MOTHER', 'MARTIAN', 'MILK', 'SHIBA', 'AKITA'
]

# Перспективные низкокап альты (не мемы)
PROMISING_LOWCAPS = [
    'AI', 'ARB', 'OP', 'APT', 'SUI', 'SEI', 'TIA', 'INJ', 'RNDR', 'FET', 
    'AGIX', 'OCEAN', 'NMR', 'LINK', 'BAND', 'DIA', 'TRB', 'UMA', 'API3',
    'GRT', 'LPT', 'LQTY', 'CRV', 'FXS', 'BAL', 'SNX', 'SUSHI', 'CAKE',
    'DYDX', 'PERP', 'GMX', 'GNS', 'VELA', 'RPL', 'LDO', 'FXS', 'FIS',
    'AAVE', 'COMP', 'MKR', 'YFI', 'ALPHA', 'ENS', 'RARE', 'SUPER', 'TVK',
    'SAND', 'MANA', 'GALA', 'ENJ', 'AXS', 'SLP', 'ILV', 'YGG', 'MC',
    'MATIC', 'AVAX', 'FTM', 'ONE', 'ALGO', 'NEAR', 'ATOM', 'OSMO', 'JUNO',
    'EVMOS', 'STRD', 'INJ', 'KUJI', 'SCRT', 'STARS', 'HUAHUA', 'BOOT',
    'CORE', 'CFX', 'MINA', 'ROSE', 'CELO', 'MOONBEAM', 'MOVR', 'GLMR',
    'ASTR', 'SDN', 'AUDIO', 'WAVES', 'KDA', 'FLOW', 'IMX', 'SYS', 'METIS',
    'KAVA', 'EGLD', 'ZIL', 'IOTA', 'HIVE', 'STEEM', 'BTS', 'ONT', 'VET',
    'THETA', 'TFUEL', 'HOT', 'IOST', 'NEO', 'GAS', 'ONT', 'VTHO', 'ICX',
    'ZEN', 'SC', 'XDC', 'ALEPH', 'PHA', 'DOCK', 'OCEAN', 'NKN', 'ANKR',
    'COTI', 'DENT', 'HBAR', 'STMX', 'CHR', 'REQ', 'NMR', 'POLY', 'CVC'
]

# Исключения - слишком крупные монеты
LARGE_CAP_EXCLUSIONS = [
    'BTC', 'ETH', 'BNB', 'XRP', 'ADA', 'SOL', 'DOT', 'LTC', 'BCH', 'XLM',
    'LINK', 'ATOM', 'XMR', 'ETC', 'XTZ', 'EOS', 'AAVE', 'ALGO', 'AVAX',
    'AXS', 'BAT', 'COMP', 'DASH', 'ENJ', 'FIL', 'GRT', 'ICP', 'KSM', 'MANA',
    'MKR', 'NEAR', 'SAND', 'SNX', 'UNI', 'YFI', 'ZEC', 'KAVA', 'RUNE'
]

# ========================= Классификация монет =========================

def classify_symbol(symbol: str, market_data: Dict) -> str:
    """Классификация монеты по категориям"""
    base_symbol = symbol.split('/')[0] if '/' in symbol else symbol
    
    # Проверка на крупные капы
    if base_symbol in LARGE_CAP_EXCLUSIONS:
        return "largecap"
    
    # Проверка на мемкоины
    if is_meme_coin(symbol):
        return "meme"
    
    # Проверка на перспективные низкокапы
    if base_symbol in PROMISING_LOWCAPS:
        return "promising_lowcap"
    
    # Все остальные - другие альты
    return "other_alt"

def is_meme_coin(symbol: str) -> bool:
    """Проверка на мемкоин"""
    base_symbol = symbol.split('/')[0] if '/' in symbol else symbol
    
    # Проверка по ключевым словам
    for keyword in MEME_KEYWORDS:
        if keyword in base_symbol.upper():
            return True
    
    # Дополнительные паттерны
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
            
            # Базовые фильтры ликвидности
            ticker = tickers.get(symbol, {})
            quote_volume = float(ticker.get('quoteVolume', 0))
            last_price = float(ticker.get('last', 0))
            
            if quote_volume < 100000 or last_price < 0.0001:
                continue
            
            # Классификация
            category = classify_symbol(symbol, market)
            
            # Фильтр по капитализации (грубая оценка)
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
            # Очень грубая оценка
            return base_volume * last_price * 3
    except Exception:
        pass
    return None

# ========================= Анализ сигналов =========================

def analyze_quality_signal(symbol: str, category: str, exchange, ohlcv_5m: List, ohlcv_15m: List, ticker: Dict) -> Optional[Dict[str, Any]]:
    """Анализ качественного сигнала с учетом категории"""
    try:
        current_price = ticker['last']
        
        # Анализ силы пампа
        pump_strength = analyze_pump_strength(ohlcv_5m, ohlcv_5m)
        
        # Разные параметры для разных категорий
        if category == "meme":
            min_pump = PUMP_THRESHOLD
            min_rsi = RSI_OVERBOUGHT
            min_volume = VOLUME_SPIKE_RATIO
        elif category == "promising_lowcap":
            min_pump = PUMP_THRESHOLD * 0.8  # Более мягкие условия для перспективных
            min_rsi = RSI_OVERBOUGHT - 5
            min_volume = VOLUME_SPIKE_RATIO * 0.8
        else:  # other_alt
            min_pump = PUMP_THRESHOLD
            min_rsi = RSI_OVERBOUGHT
            min_volume = VOLUME_SPIKE_RATIO
        
        # Проверяем основные условия
        if not (pump_strength["strength"] >= min_pump and 
                pump_strength["rsi"] >= min_rsi and
                pump_strength["volume_ratio"] >= min_volume):
            return None
        
        # Находим экстремумы пампа
        pump_high = max([x[2] for x in ohlcv_5m[-6:]])
        pump_low = min([x[3] for x in ohlcv_5m[-12:-6]])
        
        total_move = pump_high - pump_low
        if total_move <= 0:
            return None
            
        current_retrace = (pump_high - current_price) / total_move * 100
        
        if not (MIN_RETRACEMENT <= current_retrace <= MAX_RETRACEMENT):
            return None
        
        # Анализ дополнительных факторов
        confidence = calculate_confidence(ohlcv_15m, current_retrace, pump_strength, category)
        
        # Бонус уверенности для перспективных низкокапов
        if category == "promising_lowcap":
            confidence += 5
        
        # Расчет целей
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
    
    # Базовые факторы
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
    
    # Бонусы за категорию
    if category == "promising_lowcap":
        confidence += 5
    elif category == "meme":
        # Мемы более волатильны, немного снижаем уверенность
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
    
    # Эмодзи для категорий
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
    
    message = (
        f"🎯 <b>СИГНАЛ ДЛЯ ВХОДА</b> 🎯\n"
        f"{emoji} <b>Категория:</b> {cat_name}\n\n"
        
        f"<b>Монета:</b> {symbol}\n"
        f"<b>Направление:</b> SHORT 🐻\n"
        f"<b>Тип:</b> Откат после пампа\n\n"
        
        f"📊 <b>СТАТИСТИКА ПАМПА:</b>\n"
        f"• Сила пампа: <b>{signal['pump_strength']:.1f}%</b>\n"
        f"• RSI: <b>{signal['rsi']:.1f}</b> (перекупленность)\n"
        f"• Объем: <b>x{signal['volume_ratio']:.1f}</b> от среднего\n"
        f"• Откат: <b>{signal['current_retrace']:.1f}%</b> от движения\n\n"
        
        f"💎 <b>ПАРАМЕТРЫ ВХОДА:</b>\n"
        f"• Цена входа: <b>{entry:.6f}</b>\n"
        f"• Стоп-лосс: <b>{stop:.6f}</b> (+{RECOMMENDED_STOP_LOSS}%)\n"
        f"• Тейк-профит: <b>{take:.6f}</b> (-{RECOMMENDED_TAKE_PROFIT}%)\n"
        f"• Плечо: <b>{signal['leverage']}x</b>\n"
        f"• Risk/Reward: <b>1:{signal['risk_reward']:.1f}</b>\n\n"
        
        f"📈 <b>УРОВНИ ФИБОНАЧЧИ:</b>\n"
    )
    
    for level, price in signal["fib_levels"].items():
        message += f"• {level}: <b>{price:.6f}</b>\n"
    
    message += f"\n"
    message += f"⚡ <b>УВЕРЕННОСТЬ:</b> <b>{signal['confidence']:.0f}%</b>\n"
    message += f"🕒 <b>Время:</b> {datetime.now().strftime('%H:%M:%S')}\n\n"
    
    # Разные предупреждения для разных категорий
    if category == "meme":
        message += (
            f"<i>⚠️ ВНИМАНИЕ: Мемкоины - высокорисковые активы!\n"
            f"Крайняя волатильность! Используйте строгое управление капиталом!</i>"
        )
    elif category == "promising_lowcap":
        message += (
            f"<i>💡 Перспективный проект с хорошими фундаменталами.\n"
            f"Более предсказуемая волатильность.</i>"
        )
    else:
        message += (
            f"<i>📊 Стандартный альткоин.\n"
            f"Средний уровень риска.</i>"
        )
    
    return message

# ========================= Основной цикл =========================

def main():
    print("Запуск сигнального бота для всех категорий альтов...")
    
    exchange = ccxt.bybit({"enableRateLimit": True})
    recent_signals = {}
    signal_cooldown = 60
    
    # Получаем символы по категориям
    categorized_symbols = get_symbols_by_category(exchange)
    
    total_symbols = sum(len(symbols) for symbols in categorized_symbols.values())
    
    send_telegram(
        f"✅ <b>УНИВЕРСАЛЬНЫЙ СИГНАЛЬНЫЙ БОТ ЗАПУЩЕН</b>\n"
        f"<b>Охват категорий:</b>\n"
        f"• 🐶 Мемкоины: {len(categorized_symbols['meme'])}\n"
        f"• 🚀 Перспективные низкокапы: {len(categorized_symbols['promising_lowcap'])}\n"
        f"• 💎 Другие альты: {len(categorized_symbols['other_alt'])}\n"
        f"• 📊 Крупные капы: {len(categorized_symbols['largecap']} (исключены)\n\n"
        f"<b>Всего отслеживаем:</b> {total_symbols} монет\n\n"
        f"<i>Ожидаем качественные сигналы по всем категориям!</i>"
    )
    
    print(f"Найдено монет:")
    print(f"- Мемкоины: {len(categorized_symbols['meme'])}")
    print(f"- Перспективные низкокапы: {len(categorized_symbols['promising_lowcap'])}")
    print(f"- Другие альты: {len(categorized_symbols['other_alt'])}")
    
    while True:
        try:
            quality_signals = []
            
            # Сканируем все категории кроме крупных капов
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
            
            # Сортируем и отправляем лучшие сигналы
            quality_signals.sort(key=lambda x: x["confidence"], reverse=True)
            
            for signal in quality_signals[:5]:  # Увеличили до 5 сигналов
                message = format_signal_message(signal)
                send_telegram(message)
                print(f"Отправлен сигнал {signal['category']} - {signal['symbol']} (уверенность: {signal['confidence']:.0f}%)")
                time.sleep(2)
                    
        except Exception as e:
            print(f"Ошибка основного цикла: {e}")
            time.sleep(10)
        
        print(f"Цикл завершен. Отслеживаем {total_symbols} монет...")
        time.sleep(POLL_INTERVAL_SEC)

# ========================= Вспомогательные функции =========================

def send_telegram(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID, 
        "text": text, 
        "parse_mode": "HTML", 
        "disable_web_page_preview": True
    }
    try:
        requests.post(url, json=payload, timeout=15)
    except Exception:
        pass

def check_existing_signals(symbol: str, new_signal: Dict, recent_signals: Dict, cooldown_min: int = 60) -> bool:
    if symbol in recent_signals:
        if time.time() - recent_signals[symbol] < cooldown_min * 60:
            return False
    recent_signals[symbol] = time.time()
    return True

def analyze_pump_strength(ohlcv: List, volume_data: List) -> Dict[str, Any]:
    # Реализация из предыдущего кода
    pass

def calculate_rsi(prices: List[float], period: int = 14) -> float:
    # Реализация из предыдущего кода  
    pass

def calculate_fibonacci_levels(low: float, high: float) -> Dict[str, float]:
    # Реализация из предыдущего кода
    pass

if __name__ == "__main__":
    main()
