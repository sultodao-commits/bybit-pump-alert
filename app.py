#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bybit Futures Signals Bot - STRICT 20/DAY, PUMP>10%/1H
"""

import os
import time
import requests
import ccxt
from typing import List, Dict, Any, Optional

# ========================= ОПТИМАЛЬНЫЕ НАСТРОЙКИ =========================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Жёсткие фильтры для очень чётких сигналов
MIN_PUMP_STRENGTH = 10.0       # Требуем памп >= 10% за 1 час (12х5m свечей)
MAX_PULLBACK_FROM_HIGH = 2.5   # Макс откат от пика 2.5%
MIN_RSI = 72                   # RSI от 72 (можно поднять при желании)
VOLUME_DECREASE = 0.75         # Текущий объём ≤0.75x от пика

# Торговые параметры
TARGET_DUMP = 10               # Цель -10%
STOP_LOSS = 3.5                # Стоп-лосс +3.5%
LEVERAGE = 4                   # Плечо 4x

# Ограничения по количеству
MAX_DAILY_SIGNALS = 20         # Максимум сигналов в день

# Интервалы
POLL_INTERVAL_SEC = 25         # Сканирование каждые 25 сек
SIGNAL_COOLDOWN_MIN = 60       # Кулдаун 60 мин для каждой монеты

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
    """Оптимальный анализ разворотов — очень строгие правила:
       - Памп >= MIN_PUMP_STRENGTH за последний час (12 свечей по 5m)
       - Откат от пика <= MAX_PULLBACK_FROM_HIGH
       - RSI >= MIN_RSI
       - Объём снижается относительно пика
    """
    try:
        # Нужны как минимум 13 свечей, чтобы считать 12 свечей = 1 час
        if len(ohlcv) < 13:
            return None
        
        current_candle = ohlcv[-1]
        current_high = float(current_candle[2])
        current_low = float(current_candle[3])
        current_close = float(current_candle[4])
        current_volume = float(current_candle[5])
        current_open = float(current_candle[1])
        
        # ---------------------------------------------------------------------
        # 1) Памп за 1 час (12 последних 5m свечей)
        last_12 = ohlcv[-12:]
        highs_12 = [float(x[2]) for x in last_12]
        lows_12 = [float(x[3]) for x in last_12]
        hour_high = max(highs_12)
        hour_low = min(lows_12)
        pump_strength_hour = (hour_high - hour_low) / hour_low * 100 if hour_low > 0 else 0.0
        
        # Требуемый памп
        if pump_strength_hour < MIN_PUMP_STRENGTH:
            # Не достаточно мощный памп за час
            return None
        
        # ---------------------------------------------------------------------
        # 2) Проверяем откат от пика (относительно максимумa за последние 10 свечей для локального пика)
        recent_candles = ohlcv[-10:]
        recent_highs = [float(x[2]) for x in recent_candles]
        recent_lows = [float(x[3]) for x in recent_candles]
        absolute_high = max(recent_highs)
        absolute_low = min(recent_lows)
        
        pullback_from_high = (absolute_high - current_close) / absolute_high * 100 if absolute_high > 0 else 100.0
        if pullback_from_high > MAX_PULLBACK_FROM_HIGH:
            return None
        
        # ---------------------------------------------------------------------
        # 3) RSI
        closes = [float(x[4]) for x in ohlcv]
        rsi_current = calculate_accurate_rsi(closes)
        if rsi_current < MIN_RSI:
            return None
        
        # ---------------------------------------------------------------------
        # 4) Объём — сравниваем с пиковым в последних 10 свечах
        recent_volumes = [float(x[5]) for x in recent_candles]
        volume_peak = max(recent_volumes[:-1]) if len(recent_volumes) > 1 else current_volume
        volume_ratio = current_volume / volume_peak if volume_peak > 0 else 1.0
        
        # ---------------------------------------------------------------------
        # 5) Паттерны свечей (верхняя тень, маленькое тело => rejection)
        body = abs(current_close - current_open)
        candle_range = current_high - current_low if (current_high - current_low) > 0 else 1e-9
        upper_wick = current_high - max(current_open, current_close)
        wick_ratio = upper_wick / body if body > 0 else upper_wick / candle_range
        
        is_doji = (body / candle_range) < 0.15 if candle_range > 0 else False
        
        print(f"🔎 {symbol}: pump_1h={pump_strength_hour:.1f}%, откат={pullback_from_high:.2f}%, RSI={rsi_current:.1f}, vol_ratio={volume_ratio:.2f}, wick={wick_ratio:.2f}")
        
        # ================== Строгие критерии ==================
        conditions = {
            "pump_1h": pump_strength_hour >= MIN_PUMP_STRENGTH,
            "near_high": pullback_from_high <= MAX_PULLBACK_FROM_HIGH,
            "overbought": rsi_current >= MIN_RSI,
            "volume_decreasing": volume_ratio <= VOLUME_DECREASE,
            "rejection_wick": wick_ratio >= 0.25,
            "not_doji": not is_doji
        }
        conditions_met = sum(1 for v in conditions.values() if v)
        
        # Требуем минимум 4 условий, обязательно pump_1h + near_high + overbought
        if (conditions_met >= 4 and conditions["pump_1h"] and conditions["near_high"] and conditions["overbought"]):
            # Бонусные очки за экстремальные признаки
            bonus_score = 0
            if pump_strength_hour > 15: bonus_score += 1
            if volume_ratio < 0.6: bonus_score += 1
            if wick_ratio > 0.4: bonus_score += 1
            
            entry_price = current_close
            take_profit = absolute_high * (1 - TARGET_DUMP / 100)
            stop_loss = entry_price * (1 + STOP_LOSS / 100)
            
            confidence = 60 + (conditions_met * 6) + (bonus_score * 5)
            confidence = min(confidence, 92)
            
            if pullback_from_high <= 1.0:
                stage = "🟢 У ПИКА - разворот imminent"
            elif pullback_from_high <= 1.8:
                stage = "🟡 НАЧАЛО ОТКАТА - хорошая точка"
            else:
                stage = "🔴 ОТКАТ ИДЕТ - ещё есть потенциал"
            
            return {
                "symbol": symbol,
                "direction": "SHORT",
                "entry_price": entry_price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "pump_high": hour_high,
                "pump_strength_hour": pump_strength_hour,
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
    print("🎯 ЗАПУСК БОТА — ЛИМИТ 20 СИГНАЛОВ/ДЕНЬ, ПАМП>10%/1Ч")
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ Укажи TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в окружении!")
        return
    
    exchange = ccxt.bybit({"enableRateLimit": True})
    recent_signals = {}
    
    # Загрузка рынков
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
    
    print(f"Отслеживаем {len(symbols)} монет (приоритет — волатильные)")
    
    send_telegram(
        f"⚡ <b>БОТ ЗАПУЩЕН — ЖЁСТКИЙ РЕЖИМ</b>\n\n"
        f"• Лимит сигналов в день: {MAX_DAILY_SIGNALS}\n"
        f"• Требование: памп ≥{MIN_PUMP_STRENGTH}% за 1 час (12×5m)\n"
        f"• Откат от пика ≤{MAX_PULLBACK_FROM_HIGH}%\n"
        f"• RSI ≥{MIN_RSI}\n"
    )
    
    daily_signals = 0
    last_reset = time.time()
    
    while True:
        try:
            # Сброс счётчика каждые 24 часа
            if time.time() - last_reset > 86400:
                daily_signals = 0
                last_reset = time.time()
                print("🔄 Сброс дневного счетчика сигналов")
            
            if daily_signals >= MAX_DAILY_SIGNALS:
                print(f"🏁 Дневной лимит {MAX_DAILY_SIGNALS} достигнут. Сплю {POLL_INTERVAL_SEC}s перед новой проверкой.")
                time.sleep(POLL_INTERVAL_SEC)
                continue
            
            signals_found = 0
            print(f"\n🔄 Сканируем... | Сегодня отправлено: {daily_signals} сигналов")
            
            for symbol in symbols:
                # Если достигли лимита — выходим из цикла проверки
                if daily_signals >= MAX_DAILY_SIGNALS:
                    break
                try:
                    # Берём 15 свечей по 5m (хватает для расчётов)
                    ohlcv = exchange.fetch_ohlcv(symbol, '5m', limit=15)
                    ticker = exchange.fetch_ticker(symbol)
                    
                    if not ohlcv or len(ohlcv) < 13:
                        continue
                    
                    signal = analyze_optimal_reversal(symbol, ohlcv, ticker)
                    
                    if signal:
                        # Кулдаун по монете
                        signal_key = symbol
                        current_time = time.time()
                        
                        if signal_key in recent_signals:
                            if (current_time - recent_signals[signal_key]) < SIGNAL_COOLDOWN_MIN * 60:
                                continue
                        
                        # Дополнительная защита: если дневной лимит уже почти исчерпан — проверяем
                        if daily_signals >= MAX_DAILY_SIGNALS:
                            break
                        
                        recent_signals[signal_key] = current_time
                        send_telegram(format_signal_message(signal))
                        daily_signals += 1
                        signals_found += 1
                        print(f"🎯 СИГНАЛ #{daily_signals}: {symbol} | pump1h={signal['pump_strength_hour']:.1f}% | уверенность {signal['confidence']}%")
                    
                    # небольшая пауза между запросами к API
                    time.sleep(0.02)
                    
                except Exception as e:
                    # Логируем и продолжаем
                    # print(f"Ошибка по {symbol}: {e}")
                    continue
            
            # Очистка старых записей кулдауна (в два раза больше кулдауна)
            current_time = time.time()
            recent_signals = {k: v for k, v in recent_signals.items() 
                              if current_time - v < SIGNAL_COOLDOWN_MIN * 60 * 2}
            
            if signals_found > 0:
                print(f"Найдено сигналов в этом цикле: {signals_found} | Всего сегодня: {daily_signals}")
            else:
                print("⏳ Сигналов нет в этом цикле")
                    
        except Exception as e:
            print(f"💥 Ошибка основного цикла: {e}")
            time.sleep(10)
        
        print(f"⏰ Следующий цикл через {POLL_INTERVAL_SEC} сек...")
        time.sleep(POLL_INTERVAL_SEC)

def format_signal_message(signal: Dict) -> str:
    return (
        f"🎯 <b>ОПТИМАЛЬНЫЙ РАЗВОРОТ — ЖЁСТКИЙ</b>\n\n"
        f"<b>Монета:</b> {signal['symbol']}\n"
        f"<b>Стадия:</b> {signal['stage']}\n"
        f"<b>Направление:</b> SHORT 🐻\n"
        f"<b>Уверенность:</b> {signal['confidence']}%\n\n"
        f"<b>АНАЛИЗ:</b>\n"
        f"• Памп за 1ч: {signal['pump_strength_hour']:.1f}%\n"
        f"• Лок. пeк: {signal['pump_high']:.6f}\n"
        f"• От пика: {signal['pullback_from_high']:.2f}%\n"
        f"• RSI: {signal['rsi']:.1f}\n"
        f"• Объем: x{signal['volume_ratio']:.2f}\n"
        f"• Условий: {signal['conditions_met']} (+{signal['bonus_score']} бонус)\n\n"
        f"<b>ТОРГОВЛЯ:</b>\n"
        f"• Вход: {signal['entry_price']:.6f}\n"
        f"• Цель: {signal['take_profit']:.6f} (-{TARGET_DUMP}%)\n"
        f"• Стоп: {signal['stop_loss']:.6f} (+{STOP_LOSS}%)\n"
        f"• Плечо: {signal['leverage']}x\n\n"
        f"<i>⚡ Строгий режим — максимум {MAX_DAILY_SIGNALS} сигналов в сутки, памп>= {MIN_PUMP_STRENGTH}% за 1ч</i>"
    )

def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception:
        pass

if __name__ == "__main__":
    main()
