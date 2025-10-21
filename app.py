#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bybit Futures Signals Bot - Immediate Short on Pumps
Оптимизированная версия с мягкими фильтрами
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

# Telegram настройки
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# ОПТИМИЗИРОВАННЫЕ НАСТРОЙКИ - МЯГЧЕ ФИЛЬТРЫ
PUMP_THRESHOLD = 5           # Памп от 5% (было 8)
RSI_OVERBOUGHT = 70          # RSI от 70 (было 78)
VOLUME_SPIKE_RATIO = 1.5     # Объем от 1.5x (было 2.0)

# Торговые параметры
TARGET_DUMP = 10             # Цель -10% от пика пампа
STOP_LOSS = 4                # Стоп-лосс +4% от входа
LEVERAGE = 8                 # Плечо 8x

# Более мягкие фильтры монет
MAX_MARKET_CAP = 10000000000  # Макс капитализация $10B (было $5B)
MIN_MARKET_CAP = 5000000      # Мин капитализация $5M (было $10M)
MIN_24H_VOLUME = 50000        # Мин объем $50K (было $100K)

# Интервалы
POLL_INTERVAL_SEC = 20        # Интервал сканирования 20 сек (было 30)
SIGNAL_COOLDOWN_MIN = 30      # Кулдаун на монету 30 мин (было 60)

# ========================= КАТЕГОРИИ МОНЕТ =========================

MEME_KEYWORDS = [
    'DOGE', 'SHIB', 'PEPE', 'FLOKI', 'BONK', 'MEME', 'WIF', 'BOME', 'BABYDOGE',
    'ELON', 'DOG', 'CAT', 'HAM', 'TURBO', 'AIDOGE', 'AISHIB', 'PENGU', 'MOCHI',
    'WOJAK', 'KABOSU', 'KISHU', 'SAMO', 'SNEK', 'POPCAT', 'LILY', 'MOG', 'TOSHI',
    'HIPO', 'CHAD', 'GROK', 'LADYS', 'VOY', 'COQ', 'KERMIT', 'SPX', 'TRUMP',
    'BODEN', 'TREMP', 'SC', 'SMURFCAT', 'ANDY', 'WEN', 'MYRO', 'WU', 'MICHI',
    'NUB', 'DAVE', 'PONKE', 'MON', 'PUDGY', 'POWELL', 'PENG', 'SATOSHI', 'VITALIK',
    # Добавляем больше мемов
    'FART', 'POOP', 'PEE', 'CUM', 'ASS', 'BOOB', 'BUTT', 'DICK', 'WEED', 'BEER',
    'WINE', 'VODKA', 'WHISKEY', 'COKE', 'PEPSI', 'COFFEE', 'TEA', 'PIZZA', 'BURGER',
    'TACO', 'SUSHI', 'RAMEN', 'TOAST', 'BAGEL', 'DONUT', 'CAKE', 'COOKIE', 'CANDY'
]

PROMISING_LOWCAPS = [
    'AI', 'ARB', 'OP', 'APT', 'SUI', 'SEI', 'TIA', 'INJ', 'RNDR', 'FET', 
    'AGIX', 'OCEAN', 'NMR', 'LINK', 'BAND', 'DIA', 'TRB', 'UMA', 'API3',
    'GRT', 'LPT', 'LQTY', 'CRV', 'FXS', 'BAL', 'SNX', 'SUSHI', 'CAKE',
    'DYDX', 'PERP', 'GMX', 'GNS', 'VELA', 'RPL', 'LDO', 'FXS', 'FIS',
    'AAVE', 'COMP', 'MKR', 'YFI', 'ALPHA', 'ENS', 'RARE', 'SUPER', 'TVK',
    'SAND', 'MANA', 'GALA', 'ENJ', 'AXS', 'SLP', 'ILV', 'YGG', 'MC',
    'MATIC', 'AVAX', 'FTM', 'ONE', 'ALGO', 'NEAR', 'ATOM', 'OSMO', 'JUNO',
    # Добавляем больше альтов
    'RUNE', 'KAVA', 'EGLD', 'ZIL', 'IOTA', 'HIVE', 'STEEM', 'BTS', 'ONT', 'VET',
    'THETA', 'TFUEL', 'HOT', 'IOST', 'NEO', 'GAS', 'ICX', 'ZEN', 'SC', 'XDC'
]

LARGE_CAP_EXCLUSIONS = [
    'BTC', 'ETH', 'BNB', 'XRP', 'ADA', 'SOL', 'DOT', 'LTC', 'BCH', 'XLM',
    'LINK', 'ATOM', 'XMR', 'ETC', 'XTZ', 'EOS', 'AAVE', 'ALGO', 'AVAX',
    'AXS', 'BAT', 'COMP', 'DASH', 'ENJ', 'FIL', 'GRT', 'ICP', 'KSM', 'MANA'
]

# ========================= СИСТЕМА ОТЛАДКИ =========================

class DebugStats:
    def __init__(self):
        self.reset()
    
    def reset(self):
        self.total_scanned = 0
        self.pump_detected = 0
        self.rsi_passed = 0
        self.volume_passed = 0
        self.all_conditions_passed = 0
        self.signals_sent = 0
        
    def print_stats(self):
        print(f"🔍 ОТЛАДКА: Сканировано: {self.total_scanned}, "
              f"Пампы: {self.pump_detected}, RSI: {self.rsi_passed}, "
              f"Объем: {self.volume_passed}, Сигналы: {self.all_conditions_passed}")

debug_stats = DebugStats()

# ========================= КЛАССИФИКАЦИЯ МОНЕТ =========================

def classify_symbol(symbol: str) -> str:
    base_symbol = symbol.split('/')[0] if '/' in symbol else symbol
    
    if base_symbol in LARGE_CAP_EXCLUSIONS:
        return "largecap"
    
    if is_meme_coin(symbol):
        return "meme"
    
    if base_symbol in PROMISING_LOWCAPS:
        return "promising_lowcap"
    
    return "other_alt"

def is_meme_coin(symbol: str) -> bool:
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
        re.compile(r'.*FART.*', re.IGNORECASE),
        re.compile(r'.*POOP.*', re.IGNORECASE),
        re.compile(r'.*ASS.*', re.IGNORECASE),
    ]
    
    for pattern in meme_patterns:
        if pattern.match(base_symbol):
            return True
    
    return False

def get_symbols_by_category(exchange) -> Dict[str, List[str]]:
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
            
            if quote_volume < MIN_24H_VOLUME or last_price < 0.00001:
                continue
            
            category = classify_symbol(symbol)
            
            estimated_mcap = get_market_cap_estimate(ticker)
            if estimated_mcap and estimated_mcap > MAX_MARKET_CAP:
                continue
            
            categorized[category].append(symbol)
            
        except Exception:
            continue
    
    return categorized

def get_market_cap_estimate(ticker_data: Dict) -> Optional[float]:
    try:
        last_price = float(ticker_data.get('last', 0))
        base_volume = float(ticker_data.get('baseVolume', 0))
        
        if last_price > 0 and base_volume > 0:
            return base_volume * last_price * 2  # Еще более грубая оценка
    except Exception:
        pass
    return None

# ========================= АНАЛИЗ СИГНАЛОВ =========================

def analyze_pump_strength(ohlcv: List, volume_data: List) -> Dict[str, Any]:
    if len(ohlcv) < 5:  # Уменьшил минимальное количество свечей
        return {"strength": 0, "volume_spike": False, "rsi": 50, "volume_ratio": 1}
    
    # Анализ цены за последние 2 свечи (быстрее реакция)
    price_changes = []
    for i in range(1, 3):  # Только 2 свечи
        if len(ohlcv) > i:
            change = (ohlcv[-1][4] - ohlcv[-1-i][4]) / ohlcv[-1-i][4] * 100
            price_changes.append(change)
    
    # RSI с меньшим периодом для быстрой реакции
    closes = [x[4] for x in ohlcv[-10:]]  # 10 периодов вместо 14
    rsi_val = calculate_rsi(closes, 10)  # RSI 10 периодов
    
    # Volume spike
    avg_volume = sum([x[5] for x in volume_data[-15:-1]]) / 14 if len(volume_data) >= 15 else volume_data[-1][5]
    volume_ratio = volume_data[-1][5] / avg_volume if avg_volume > 0 else 1
    
    strength = sum(price_changes) / len(price_changes) if price_changes else 0
    
    return {
        "strength": strength,
        "volume_spike": volume_ratio > VOLUME_SPIKE_RATIO,
        "rsi": rsi_val,
        "volume_ratio": volume_ratio
    }

def calculate_rsi(prices: List[float], period: int = 10) -> float:
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
    try:
        current_price = ticker['last']
        pump_strength = analyze_pump_strength(ohlcv_5m, ohlcv_5m)
        
        debug_stats.total_scanned += 1
        
        # ОТЛАДОЧНАЯ ИНФОРМАЦИЯ
        pump_passed = pump_strength["strength"] >= PUMP_THRESHOLD
        rsi_passed = pump_strength["rsi"] >= RSI_OVERBOUGHT
        volume_passed = pump_strength["volume_ratio"] >= VOLUME_SPIKE_RATIO
        
        if pump_passed:
            debug_stats.pump_detected += 1
        if rsi_passed:
            debug_stats.rsi_passed += 1
        if volume_passed:
            debug_stats.volume_passed += 1
        
        # РАЗНЫЕ НАСТРОЙКИ ДЛЯ КАТЕГОРИЙ
        if category == "meme":
            # Для мемов - самые мягкие настройки
            min_pump = PUMP_THRESHOLD
            min_rsi = RSI_OVERBOUGHT - 3  # 67 для мемов
            min_volume = VOLUME_SPIKE_RATIO
        elif category == "promising_lowcap":
            # Для перспективных - средние настройки
            min_pump = PUMP_THRESHOLD
            min_rsi = RSI_OVERBOUGHT - 2  # 68
            min_volume = VOLUME_SPIKE_RATIO
        else:
            # Для остальных - стандартные
            min_pump = PUMP_THRESHOLD
            min_rsi = RSI_OVERBOUGHT
            min_volume = VOLUME_SPIKE_RATIO
        
        # Условия для входа
        if (pump_strength["strength"] >= min_pump and 
            pump_strength["rsi"] >= min_rsi and
            pump_strength["volume_ratio"] >= min_volume):
            
            debug_stats.all_conditions_passed += 1
            
            # Находим пик пампа
            pump_high = max([x[2] for x in ohlcv_5m[-4:]])  # Более короткий период
            
            # ВХОДИМ СЕЙЧАС по текущей цене!
            entry_price = current_price
            
            # Цели от ПИКА пампа
            take_profit = pump_high * (1 - TARGET_DUMP / 100)
            stop_loss = entry_price * (1 + STOP_LOSS / 100)
            
            # Расчет потенциала прибыли от пика
            potential_profit_pct = (pump_high - entry_price) / pump_high * 100
            
            # Уверенность в сигнале (более мягкая)
            confidence = calculate_confidence(pump_strength, potential_profit_pct, category)
            
            # МИНИМАЛЬНАЯ УВЕРЕННОСТЬ 50% вместо 60%
            if confidence >= 50:
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
        return None

def calculate_confidence(pump_strength: Dict, potential_profit: float, category: str) -> float:
    confidence = 50  # Старт с 50% вместо 0
    
    # Сила пампа
    if pump_strength["strength"] >= 8:
        confidence += 20
    elif pump_strength["strength"] >= 6:
        confidence += 15
    elif pump_strength["strength"] >= 5:
        confidence += 10
    
    # RSI
    if pump_strength["rsi"] >= 75:
        confidence += 15
    elif pump_strength["rsi"] >= 70:
        confidence += 10
    
    # Объем
    if pump_strength["volume_ratio"] >= 3:
        confidence += 15
    elif pump_strength["volume_ratio"] >= 2:
        confidence += 10
    elif pump_strength["volume_ratio"] >= 1.5:
        confidence += 5
    
    # Потенциальная прибыль
    if potential_profit >= 6:
        confidence += 10
    elif potential_profit >= 4:
        confidence += 5
    
    # Бонус за категорию
    if category == "meme":
        confidence += 10  # Мемы хорошо дампают
    
    return min(confidence, 95)

# ========================= ОСНОВНОЙ ЦИКЛ =========================

def send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
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
    except Exception:
        pass

def check_existing_signals(symbol: str, recent_signals: Dict) -> bool:
    if symbol in recent_signals:
        if time.time() - recent_signals[symbol] < SIGNAL_COOLDOWN_MIN * 60:
            return False
    
    recent_signals[symbol] = time.time()
    return True

def main():
    print("🚀 ЗАПУСК ОПТИМИЗИРОВАННОГО БОТА С МЯГКИМИ ФИЛЬТРАМИ...")
    
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ ОШИБКА: Укажи TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID!")
        return
    
    exchange = ccxt.bybit({"enableRateLimit": True})
    recent_signals = {}
    
    # Получаем символы по категориям
    categorized_symbols = get_symbols_by_category(exchange)
    
    total_symbols = sum(len(symbols) for symbols in categorized_symbols.values() if symbols)
    
    send_telegram(
        f"🔥 <b>БОТ ЗАПУЩЕН - ОПТИМИЗИРОВАННЫЕ НАСТРОЙКИ</b>\n"
        f"<b>Фильтры:</b> Памп ≥{PUMP_THRESHOLD}% | RSI ≥{RSI_OVERBOUGHT} | Объем ≥{VOLUME_SPIKE_RATIO}x\n"
        f"<b>Цель:</b> -{TARGET_DUMP}% от пика | <b>Плечо:</b> {LEVERAGE}x\n"
        f"<b>Охват:</b> {total_symbols} монет\n\n"
        f"<i>⚡ Мягкие фильтры - ожидаем сигналы!</i>"
    )
    
    print(f"📊 Найдено монет:")
    print(f"- Мемкоины: {len(categorized_symbols['meme'])}")
    print(f"- Перспективные низкокапы: {len(categorized_symbols['promising_lowcap'])}")
    print(f"- Другие альты: {len(categorized_symbols['other_alt'])}")
    print(f"🎯 Всего отслеживаем: {total_symbols} монет")
    
    cycle_count = 0
    
    while True:
        try:
            cycle_count += 1
            debug_stats.reset()
            quality_signals = []
            
            print(f"\n🔄 Цикл #{cycle_count} - начинаем сканирование...")
            
            for category in ["meme", "promising_lowcap", "other_alt"]:
                symbols = categorized_symbols[category]
                
                for symbol in symbols:
                    try:
                        ohlcv_5m = exchange.fetch_ohlcv(symbol, '5m', limit=20)  # Меньше данных для скорости
                        ohlcv_15m = exchange.fetch_ohlcv(symbol, '15m', limit=10)
                        ticker = exchange.fetch_ticker(symbol)
                        
                        if not ohlcv_5m or not ohlcv_15m:
                            continue
                        
                        signal = analyze_quality_signal(symbol, category, exchange, ohlcv_5m, ohlcv_15m, ticker)
                        
                        if signal:
                            if check_existing_signals(symbol, recent_signals):
                                quality_signals.append(signal)
                                debug_stats.signals_sent += 1
                        
                        time.sleep(0.03)  # Меньше задержка
                        
                    except Exception:
                        continue
            
            # Вывод отладочной информации
            debug_stats.print_stats()
            
            # Сортируем по уверенности и отправляем ВСЕ сигналы
            quality_signals.sort(key=lambda x: x["confidence"], reverse=True)
            
            for signal in quality_signals:
                message = format_signal_message(signal)
                send_telegram(message)
                print(f"📢 ОТПРАВЛЕН СИГНАЛ: {signal['category']} - {signal['symbol']} "
                      f"(памп: {signal['pump_strength']:.1f}%, RSI: {signal['rsi']:.1f}, уверенность: {signal['confidence']:.0f}%)")
                time.sleep(1)
            
            if quality_signals:
                print(f"🎉 НАЙДЕНО СИГНАЛОВ: {len(quality_signals)}")
            else:
                print("❌ Сигналов не найдено в этом цикле")
                    
        except Exception as e:
            print(f"💥 Ошибка основного цикла: {e}")
            time.sleep(10)
        
        print(f"⏰ Цикл завершен. Следующий через {POLL_INTERVAL_SEC} сек...")
        time.sleep(POLL_INTERVAL_SEC)

def format_signal_message(signal: Dict) -> str:
    symbol = signal["symbol"]
    category = signal["category"]
    entry = signal["entry_price"]
    stop = signal["stop_loss"]
    take = signal["take_profit"]
    pump_high = signal["pump_high"]
    
    category_emojis = {"meme": "🐶", "promising_lowcap": "🚀", "other_alt": "💎"}
    category_name = {"meme": "Мемкоин", "promising_lowcap": "Перспективный", "other_alt": "Альткоин"}
    
    emoji = category_emojis.get(category, "📊")
    cat_name = category_name.get(category, "Альткоин")
    
    message = (
        f"🎯 <b>СИГНАЛ ДЛЯ ВХОДА</b> 🎯\n"
        f"{emoji} <b>Категория:</b> {cat_name}\n\n"
        
        f"<b>Монета:</b> {symbol}\n"
        f"<b>Направление:</b> SHORT 🐻\n\n"
        
        f"📊 <b>ДАННЫЕ ПАМПА:</b>\n"
        f"• Сила: <b>{signal['pump_strength']:.1f}%</b>\n"
        f"• RSI: <b>{signal['rsi']:.1f}</b>\n"
        f"• Объем: <b>x{signal['volume_ratio']:.1f}</b>\n"
        f"• Пик: <b>{pump_high:.6f}</b>\n\n"
        
        f"💎 <b>ПАРАМЕТРЫ:</b>\n"
        f"• Вход: <b>{entry:.6f}</b>\n"
        f"• Стоп: <b>{stop:.6f}</b>\n"
        f"• Тейк: <b>{take:.6f}</b>\n"
        f"• Плечо: <b>{LEVERAGE}x</b>\n"
        f"• R/R: <b>1:{signal['risk_reward']:.1f}</b>\n\n"
        
        f"⚡ <b>УВЕРЕННОСТЬ:</b> <b>{signal['confidence']:.0f}%</b>\n"
    )
    
    return message

if __name__ == "__main__":
    main()
