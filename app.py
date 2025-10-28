#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bybit Futures Signals Bot - STRICT MODE
Без отката от пика, без торговли
20 точных сигналов в день, памп >10% / 1ч
"""

import os
import time
import requests
import ccxt
from typing import List, Dict, Any, Optional

# ========================= НАСТРОЙКИ =========================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Фильтры
MIN_PUMP_STRENGTH = 10.0       # Памп ≥ 10% за 1 час
MIN_RSI = 72                   # RSI ≥ 72
VOLUME_DECREASE = 0.75         # Объём ≤ 0.75x от пика

# Ограничения
MAX_DAILY_SIGNALS = 20         # Макс. сигналов в день
SIGNAL_COOLDOWN_MIN = 60       # Кулдаун 60 мин
POLL_INTERVAL_SEC = 25         # Интервал сканирования

# =============================================================

def calculate_accurate_rsi(prices: List[float], period: int = 14) -> float:
    """Точный расчёт RSI"""
    if len(prices) < period + 1:
        return 50.0
    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = [d for d in deltas if d > 0]
    losses = [-d for d in deltas if d < 0]
    if not gains and not losses:
        return 50.0
    avg_gain = sum(gains[-period:]) / period if gains else 0
    avg_loss = sum(losses[-period:]) / period if losses else 0.0001
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return min(max(rsi, 0), 100)

def analyze_strict_signal(symbol: str, ohlcv: List, ticker: Dict) -> Optional[Dict[str, Any]]:
    """Анализ без понятия 'откат от пика' и без торговли"""
    try:
        if len(ohlcv) < 13:
            return None

        current_candle = ohlcv[-1]
        current_high = float(current_candle[2])
        current_low = float(current_candle[3])
        current_close = float(current_candle[4])
        current_open = float(current_candle[1])
        current_volume = float(current_candle[5])

        # Памп за 1 час (12 свечей по 5 минут)
        last_12 = ohlcv[-12:]
        highs = [float(x[2]) for x in last_12]
        lows = [float(x[3]) for x in last_12]
        hour_high = max(highs)
        hour_low = min(lows)
        pump_strength_hour = (hour_high - hour_low) / hour_low * 100 if hour_low > 0 else 0.0

        if pump_strength_hour < MIN_PUMP_STRENGTH:
            return None

        # RSI
        closes = [float(x[4]) for x in ohlcv]
        rsi_current = calculate_accurate_rsi(closes)
        if rsi_current < MIN_RSI:
            return None

        # Объём
        recent_volumes = [float(x[5]) for x in ohlcv[-10:]]
        volume_peak = max(recent_volumes[:-1]) if len(recent_volumes) > 1 else current_volume
        volume_ratio = current_volume / volume_peak if volume_peak > 0 else 1.0

        # Форма свечи
        body = abs(current_close - current_open)
        candle_range = current_high - current_low if (current_high - current_low) > 0 else 1e-9
        upper_wick = current_high - max(current_open, current_close)
        wick_ratio = upper_wick / body if body > 0 else upper_wick / candle_range
        is_doji = (body / candle_range) < 0.15 if candle_range > 0 else False

        print(f"🔎 {symbol}: pump1h={pump_strength_hour:.1f}%, RSI={rsi_current:.1f}, vol={volume_ratio:.2f}, wick={wick_ratio:.2f}")

        # Условия
        conditions = {
            "pump_1h": pump_strength_hour >= MIN_PUMP_STRENGTH,
            "overbought": rsi_current >= MIN_RSI,
            "volume_decreasing": volume_ratio <= VOLUME_DECREASE,
            "rejection_wick": wick_ratio >= 0.25,
            "not_doji": not is_doji
        }
        conditions_met = sum(1 for v in conditions.values() if v)

        # Требуем минимум 3 условий
        if conditions_met >= 3:
            bonus_score = 0
            if pump_strength_hour > 15: bonus_score += 1
            if volume_ratio < 0.6: bonus_score += 1
            if wick_ratio > 0.4: bonus_score += 1

            confidence = 60 + (conditions_met * 7) + (bonus_score * 5)
            confidence = min(confidence, 95)

            return {
                "symbol": symbol,
                "pump_strength_hour": pump_strength_hour,
                "rsi": rsi_current,
                "volume_ratio": volume_ratio,
                "wick_ratio": wick_ratio,
                "confidence": confidence,
                "conditions_met": f"{conditions_met}/5",
                "bonus_score": bonus_score,
                "timestamp": time.time()
            }

        return None
    except Exception as e:
        print(f"Ошибка анализа {symbol}: {e}")
        return None

def main():
    print("🎯 ЗАПУСК СТРОГОГО СИГНАЛЬНОГО БОТА (БЕЗ ОТКАТА И ТОРГОВЛИ)")
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ Укажи TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID!")
        return

    exchange = ccxt.bybit({"enableRateLimit": True})
    recent_signals = {}
    markets = exchange.load_markets()
    symbols = []

    volatile_keywords = ["PEPE", "FLOKI", "BONK", "SHIB", "DOGE", "MEME", "BOME", "WIF", "POPCAT", "ORDI", "SATS"]

    for symbol, market in markets.items():
        try:
            if (market.get("type") == "swap" and market.get("linear")
                and market.get("settle") == "USDT" and "USDT" in symbol and "/" in symbol):
                if any(k in symbol for k in volatile_keywords):
                    symbols.insert(0, symbol)
                else:
                    symbols.append(symbol)
                if len(symbols) >= 160:
                    break
        except:
            continue

    send_telegram(
        f"⚡ <b>БОТ ЗАПУЩЕН</b>\n"
        f"• Памп ≥ {MIN_PUMP_STRENGTH}% за 1ч\n"
        f"• RSI ≥ {MIN_RSI}\n"
        f"• Объём ≤ {VOLUME_DECREASE}x\n"
        f"• Макс. сигналов в день: {MAX_DAILY_SIGNALS}\n"
        f"• Без отката и торговли"
    )

    daily_signals = 0
    last_reset = time.time()

    while True:
        try:
            # Сброс каждые 24 часа
            if time.time() - last_reset > 86400:
                daily_signals = 0
                last_reset = time.time()
                print("🔄 Сброс дневного лимита сигналов")

            if daily_signals >= MAX_DAILY_SIGNALS:
                print(f"🏁 Лимит {MAX_DAILY_SIGNALS} сигналов достигнут")
                time.sleep(POLL_INTERVAL_SEC)
                continue

            signals_found = 0
            print(f"\n🔄 Сканируем... | Сегодня: {daily_signals} сигналов")

            for symbol in symbols:
                if daily_signals >= MAX_DAILY_SIGNALS:
                    break
                try:
                    ohlcv = exchange.fetch_ohlcv(symbol, '5m', limit=15)
                    ticker = exchange.fetch_ticker(symbol)
                    if not ohlcv or len(ohlcv) < 13:
                        continue

                    signal = analyze_strict_signal(symbol, ohlcv, ticker)
                    if signal:
                        current_time = time.time()
                        if symbol in recent_signals:
                            if (current_time - recent_signals[symbol]) < SIGNAL_COOLDOWN_MIN * 60:
                                continue
                        recent_signals[symbol] = current_time
                        send_telegram(format_signal_message(signal))
                        daily_signals += 1
                        signals_found += 1
                        print(f"🎯 СИГНАЛ #{daily_signals}: {symbol} ({signal['confidence']}%)")

                    time.sleep(0.02)
                except:
                    continue

            # Очистка кулдауна
            now = time.time()
            recent_signals = {k: v for k, v in recent_signals.items()
                              if now - v < SIGNAL_COOLDOWN_MIN * 60 * 2}

            if signals_found == 0:
                print("⏳ Сигналов нет в этом цикле")

        except Exception as e:
            print(f"💥 Ошибка: {e}")
            time.sleep(10)

        print(f"⏰ Следующий цикл через {POLL_INTERVAL_SEC} сек...")
        time.sleep(POLL_INTERVAL_SEC)

def format_signal_message(signal: Dict) -> str:
    return (
        f"🎯 <b>СИГНАЛ РАЗВОРОТА</b>\n\n"
        f"<b>Монета:</b> {signal['symbol']}\n"
        f"<b>Уверенность:</b> {signal['confidence']}%\n\n"
        f"<b>Показатели:</b>\n"
        f"• Памп за 1ч: {signal['pump_strength_hour']:.1f}%\n"
        f"• RSI: {signal['rsi']:.1f}\n"
        f"• Объём: x{signal['volume_ratio']:.2f}\n"
        f"• Тень: {signal['wick_ratio']:.2f}\n"
        f"• Условий: {signal['conditions_met']} (+{signal['bonus_score']} бонус)\n\n"
        f"<i>⚡ Без торговли. Чистые сигналы на разворот.</i>"
    )

def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10
        )
    except:
        pass

if __name__ == "__main__":
    main()
