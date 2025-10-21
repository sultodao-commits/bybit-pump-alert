#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bybit Futures Signals Bot - Immediate Short on Pumps
Немедленный вход в SHORT при обнаружении пампа
"""

import os
import time
import traceback
import re
from datetime import datetime
from typing import List, Dict, Any, Optional

import requests
import ccxt

# ========================= КОНФИГУРАЦИЯ =========================

# Telegram настройки (обязательные)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Стратегия - НЕМЕДЛЕННЫЙ ВХОД В SHORT ПРИ ПАМПЕ
PUMP_THRESHOLD = 8           # Минимальный памп 8% для входа
RSI_OVERBOUGHT = 78          # RSI от 78 (перекупленность)
VOLUME_SPIKE_RATIO = 2.0     # Объем в 2x от среднего

# Торговые параметры
TARGET_DUMP = 12             # Цель -12% от пика пампа
STOP_LOSS = 3                # Стоп-лосс +3% от входа
LEVERAGE = 10                # Плечо 10x

# Фильтры монет
MAX_MARKET_CAP = 5000000000  # Макс капитализация $5B
MIN_MARKET_CAP = 10000000    # Мин капитализация $10M
MIN_24H_VOLUME = 100000      # Мин объем $100K

# Интервалы
POLL_INTERVAL_SEC = 30       # Интервал сканирования 30 сек
SIGNAL_COOLDOWN_MIN = 60     # Кулдаун на монету 60 мин

# ========================= КАТЕГОРИИ МОНЕТ =========================

MEME_KEYWORDS = [
    'DOGE', 'SHIB', 'PEPE', 'FLOKI', 'BONK', 'MEME', 'WIF', 'BOME', 'BABYDOGE',
    'ELON', 'DOG', 'CAT', 'HAM', 'TURBO', 'AIDOGE', 'AISHIB', 'PENGU', 'MOCHI',
    'WOJAK', 'KABOSU', 'KISHU', 'SAMO', 'SNEK', 'POPCAT', 'LILY', 'MOG', 'TOSHI',
    'HIPO', 'CHAD', 'GROK', 'LADYS', 'VOY', 'COQ', 'KERMIT', 'SPX', 'TRUMP',
    'BODEN', 'TREMP', 'SC', 'SMURFCAT', 'ANDY', 'WEN', 'MYRO', 'WU', 'MICHI',
    'NUB', 'DAVE', 'PONKE', 'MON', 'PUDGY', 'POWELL', 'PENG', 'SATOSHI', 'VITALIK'
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

# ========================= КЛАССИФИКАЦИЯ МОНЕТ =========================

def classify_symbol(symbol: str) -> str:
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
            
            if quote_volume < MIN_24H_VOLUME or last_price < 0.0001:
                continue
            
            category = classify_symbol(symbol)
            
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

# ========================= АНАЛИЗ СИГНАЛОВ =========================

def analyze_pump_strength(ohlcv: List, volume_data: List) -> Dict[str, Any]:
    """Анализ силы пампа"""
    if len(ohlcv) < 10:
        return {"strength": 0, "volume_spike": False, "rsi": 50, "volume_ratio": 1}
    
    # Анализ цены за последние 3 свечи
    price_changes = []
    for i in range(1, 4):
        if len(ohlcv) > i:
            change = (ohlcv[-1][4] - ohlcv[-1-i][4]) / ohlcv[-1-i][4] * 100
            price_changes.append(change)
    
    # RSI
    closes = [x[4] for x in ohlcv[-14:]]
    rsi_val = calculate_rsi(closes)
    
    # Volume spike
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

def analyze_quality_signal(symbol: str, category: str, exchange, ohlcv_5m: List, ohlcv_15m: List, ticker: Dict) -> Optional[Dict[str, Any]]:
    """Анализ для НЕМЕДЛЕННОГО входа в SHORT при пампе"""
    try:
        current_price = ticker['last']
        pump_strength = analyze_pump_strength(ohlcv_5m, ohlcv_5m)
        
        # Условия для немедленного входа
        if (pump_strength["strength"] >= PUMP_THRESHOLD and 
            pump_strength["rsi"] >= RSI_OVERBOUGHT and
            pump_strength["volume_ratio"] >= VOLUME_SPIKE_RATIO):
            
            # Находим пик пампа
            pump_high = max([x[2] for x in ohlcv_5m[-6:]])
            
            # ВХОДИМ СЕЙЧАС по текущей цене!
            entry_price = current_price
            
            # Цели от ПИКА пампа
            take_profit = pump_high * (1 - TARGET_DUMP / 100)
            stop_loss = entry_price * (1 + STOP_LOSS / 100)
            
            # Расчет потенциала прибыли от пика
            potential_profit_pct = (pump_high - entry_price) / pump_high * 100
            
            # Уверенность в сигнале
            confidence = calculate_confidence(pump_strength, potential_profit_pct, category)
            
            return {
                "symbol": symbol,
                "category": category,
                "direction": "SHORT",
                "entry_price": entry_price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "pump_high": pump_high,
                "current_price": current_price,
                "pump_strength": pump_strength["strength"],
                "rsi": pump_strength["rsi"],
                "volume_ratio": pump_strength["volume_ratio"],
                "potential_profit_pct": potential_profit_pct,
                "confidence": confidence,
                "leverage": LEVERAGE,
                "risk_reward": TARGET_DUMP / STOP_LOSS,
                "timestamp": time.time()
            }
        
        return None
        
    except Exception as e:
        print(f"Ошибка анализа сигнала {symbol}: {e}")
        return None

def calculate_confidence(pump_strength: Dict, potential_profit: float, category: str) -> float:
    """Расчет уверенности в сигнале"""
    confidence = 50
    
    # Сила пампа
    if pump_strength["strength"] >= 15:
        confidence += 20
    elif pump_strength["strength"] >= 10:
        confidence += 15
    elif pump_strength["strength"] >= 8:
        confidence += 10
    
    # RSI
    if pump_strength["rsi"] >= 85:
        confidence += 15
    elif pump_strength["rsi"] >= 80:
        confidence += 10
    elif pump_strength["rsi"] >= 78:
        confidence += 5
    
    # Объем
    if pump_strength["volume_ratio"] >= 4:
        confidence += 15
    elif pump_strength["volume_ratio"] >= 3:
        confidence += 10
    elif pump_strength["volume_ratio"] >= 2:
        confidence += 5
    
    # Потенциальная прибыль
    if potential_profit >= 8:
        confidence += 10
    elif potential_profit >= 5:
        confidence += 5
    
    # Бонус за категорию
    if category == "meme":
        confidence += 5  # Мемы хорошо дампают
    
    return min(confidence, 95)

# ========================= ФОРМАТИРОВАНИЕ СООБЩЕНИЙ =========================

def format_signal_message(signal: Dict) -> str:
    """Форматирование торгового сигнала"""
    symbol = signal["symbol"]
    category = signal["category"]
    entry = signal["entry_price"]
    stop = signal["stop_loss"]
    take = signal["take_profit"]
    pump_high = signal["pump_high"]
    
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
        f"🎯 <b>СИГНАЛ ДЛЯ НЕМЕДЛЕННОГО ВХОДА</b> 🎯\n"
        f"{emoji} <b>Категория:</b> {cat_name}\n\n"
        
        f"<b>Монета:</b> {symbol}\n"
        f"<b>Направление:</b> SHORT 🐻\n"
        f"<b>Стратегия:</b> Немедленный вход после пампа\n\n"
        
        f"📊 <b>СТАТИСТИКА ПАМПА:</b>\n"
        f"• Сила пампа: <b>{signal['pump_strength']:.1f}%</b>\n"
        f"• RSI: <b>{signal['rsi']:.1f}</b> (перекупленность)\n"
        f"• Объем: <b>x{signal['volume_ratio']:.1f}</b> от среднего\n"
        f"• Пик пампа: <b>{pump_high:.6f}</b>\n\n"
        
        f"💎 <b>ПАРАМЕТРЫ ВХОДА:</b>\n"
        f"• Цена входа: <b>{entry:.6f}</b>\n"
        f"• Стоп-лосс: <b>{stop:.6f}</b> (+{STOP_LOSS}%)\n"
        f"• Тейк-профит: <b>{take:.6f}</b> (-{TARGET_DUMP}% от пика)\n"
        f"• Плечо: <b>{LEVERAGE}x</b>\n"
        f"• Risk/Reward: <b>1:{signal['risk_reward']:.1f}</b>\n"
        f"• Потенциал от пика: <b>{signal['potential_profit_pct']:.1f}%</b>\n\n"
        
        f"⚡ <b>УВЕРЕННОСТЬ:</b> <b>{signal['confidence']:.0f}%</b>\n"
        f"🕒 <b>Время:</b> {datetime.now().strftime('%H:%M:%S')}\n\n"
    )
    
    if category == "meme":
        message += (
            f"<i>⚠️ Мемкоин! Высокая волатильность!\n"
            f"🚀 Входим СРАЗУ - ловим весь дамп!\n"
            f"💎 Используйте строгие стоп-лоссы!</i>"
        )
    else:
        message += (
            f"<i>📊 Обнаружен сильный памп с перекупленностью\n"
            f"🐻 Входим в SHORT для ловли отката\n"
            f"✅ Высокий потенциал прибыли</i>"
        )
    
    return message

# ========================= ОСНОВНОЙ ЦИКЛ =========================

def send_telegram(text: str) -> None:
    """Отправка сообщения в Telegram"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram не настроен. Пропускаем отправку.")
        return
        
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

def check_existing_signals(symbol: str, recent_signals: Dict) -> bool:
    """Проверка на дублирование сигналов"""
    if symbol in recent_signals:
        if time.time() - recent_signals[symbol] < SIGNAL_COOLDOWN_MIN * 60:
            return False
    
    recent_signals[symbol] = time.time()
    return True

def main():
    print("Запуск бота для немедленного входа в SHORT при пампе...")
    
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("ОШИБКА: Укажи TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env файле!")
        return
    
    exchange = ccxt.bybit({"enableRateLimit": True})
    recent_signals = {}
    
    # Получаем символы по категориям
    categorized_symbols = get_symbols_by_category(exchange)
    
    total_symbols = sum(len(symbols) for symbols in categorized_symbols.values() if symbols)
    
    send_telegram(
        f"✅ <b>БОТ ЗАПУЩЕН - НЕМЕДЛЕННЫЙ ВХОД В SHORT</b>\n"
        f"<b>Стратегия:</b> Вход при пампе ≥{PUMP_THRESHOLD}% с RSI ≥{RSI_OVERBOUGHT}\n"
        f"<b>Цель:</b> -{TARGET_DUMP}% от пика пампа\n"
        f"<b>Плечо:</b> {LEVERAGE}x | <b>Стоп:</b> +{STOP_LOSS}%\n\n"
        f"<b>Охват монет:</b>\n"
        f"• 🐶 Мемкоины: {len(categorized_symbols['meme'])}\n"
        f"• 🚀 Перспективные: {len(categorized_symbols['promising_lowcap'])}\n"
        f"• 💎 Другие альты: {len(categorized_symbols['other_alt'])}\n"
        f"<b>Всего:</b> {total_symbols} монет\n\n"
        f"<i>Ожидаем сильные пампы для немедленного входа в SHORT! 🐻</i>"
    )
    
    print(f"Найдено монет:")
    print(f"- Мемкоины: {len(categorized_symbols['meme'])}")
    print(f"- Перспективные низкокапы: {len(categorized_symbols['promising_lowcap'])}")
    print(f"- Другие альты: {len(categorized_symbols['other_alt'])}")
    print(f"Всего отслеживаем: {total_symbols} монет")
    
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
                            if check_existing_signals(symbol, recent_signals):
                                quality_signals.append(signal)
                        
                        time.sleep(0.05)
                        
                    except Exception as e:
                        continue
            
            # Сортируем по уверенности и отправляем лучшие
            quality_signals.sort(key=lambda x: x["confidence"], reverse=True)
            
            for signal in quality_signals[:3]:
                message = format_signal_message(signal)
                send_telegram(message)
                print(f"📢 Отправлен сигнал: {signal['category']} - {signal['symbol']} (уверенность: {signal['confidence']:.0f}%)")
                time.sleep(2)
                    
        except Exception as e:
            print(f"Ошибка основного цикла: {e}")
            traceback.print_exc()
            time.sleep(10)
        
        print(f"🔍 Цикл завершен. Следующий через {POLL_INTERVAL_SEC} сек...")
        time.sleep(POLL_INTERVAL_SEC)

if __name__ == "__main__":
    main()
