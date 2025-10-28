#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bybit Futures Signals Bot - STRICT 5M BURST WITH VOLUME
Всплески ≥10% за 5 минут с ростом объёма ≥50%
Без входов, стопов и отката от пика
Сигнал по каждой монете не повторяется в течение дня
"""

import os
import time
import requests
import ccxt
from typing import List, Dict, Any, Optional

# ========================= НАСТРОЙКИ =========================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

MIN_PUMP_STRENGTH = 10.0       # Всплеск ≥10% за 5 минут
MIN_RSI = 72                   # RSI ≥72
MIN_VOLUME_RATIO = 1.5         # Текущий объём ≥ 150% среднего предыдущих свечей
POLL_INTERVAL_SEC = 25         # Интервал проверки
SIGNAL_COOLDOWN_MIN = 18       # Кулдаун 18 мин
MAX_SIGNALS_PER_DAY = 20       # Макс. сигналов/день

# ========================= ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =========================

def calculate_accurate_rsi(prices: List[float], period: int = 14) -> float:
    if len(prices) < period + 1:
        return 50.0
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains = [d for d in deltas if d > 0]
    losses = [-d for d in deltas if d < 0]
    if not gains and not losses:
        return 50.0
    avg_gain = sum(gains[-period:]) / period if gains else 0
    avg_loss = sum(losses[-period:]) / period if losses else 0.0001
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return min(max(rsi, 0), 100)

def analyze_burst_signal(symbol: str, ohlcv: List, ticker: Dict) -> Optional[Dict[str, Any]]:
    try:
        if len(ohlcv) < 5:
            return None

        current = ohlcv[-1]
        current_close = float(current[4])
        current_open = float(current[1])
        current_high = float(current[2])
        current_low = float(current[3])
        current_volume = float(current[5])

        # === Всплеск за 5 минут (последняя свеча) ===
        pump_5min = (current_high - current_low) / current_low * 100 if current_low > 0 else 0.0
        if pump_5min < MIN_PUMP_STRENGTH:
            return None

        # === RSI ===
        closes = [float(x[4]) for x in ohlcv]
        rsi_value = calculate_accurate_rsi(closes)
        if rsi_value < MIN_RSI:
            return None

        # === Объём с ростом ≥50% ===
        recent_volumes = [float(x[5]) for x in ohlcv[-3:]]  # последние 3 свечи
        previous_avg = sum(recent_volumes[:-1]) / len(recent_volumes[:-1]) if len(recent_volumes) > 1 else current_volume
        volume_ratio = current_volume / previous_avg if previous_avg > 0 else 1
        if volume_ratio < MIN_VOLUME_RATIO:
            return None

        # === Форма свечи ===
        body = abs(current_close - current_open)
        upper_wick = current_high - max(current_open, current_close)
        wick_ratio = upper_wick / body if body > 0 else 0
        is_doji = body / (current_high - current_low) < 0.15 if (current_high - current_low) > 0 else False
        if wick_ratio < 0.25 or is_doji:
            return None

        confidence = 60 + (pump_5min / 2)
        confidence = min(confidence, 90)

        print(f"✅ {symbol}: всплеск_5м={pump_5min:.1f}%, RSI={rsi_value:.1f}, объём={volume_ratio:.2f} (+{(volume_ratio-1)*100:.0f}%), тень={wick_ratio:.2f}")

        return {
            "symbol": symbol,
            "pump_5min": pump_5min,
            "rsi": rsi_value,
            "volume_ratio": volume_ratio,
            "wick_ratio": wick_ratio,
            "confidence": confidence,
            "timestamp": time.time()
        }

    except Exception as e:
        print(f"Ошибка анализа {symbol}: {e}")
        return None

# ========================= TELEGRAM =========================

def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except:
        pass

def format_signal_message(signal: Dict) -> str:
    return (
        f"🚀 <b>СТРОГИЙ СИГНАЛ: ВСПЛЕСК ≥10% / 5м</b>\n\n"
        f"<b>Монета:</b> {signal['symbol']}\n"
        f"<b>Уверенность:</b> {signal['confidence']:.1f}%\n\n"
        f"<b>АНАЛИЗ:</b>\n"
        f"• Всплеск (5м): {signal['pump_5min']:.2f}% ⚡\n"
        f"• RSI: {signal['rsi']:.1f}\n"
        f"• Объём: x{signal['volume_ratio']:.2f} (+{(signal['volume_ratio']-1)*100:.0f}%)\n"
        f"• Верхняя тень: {signal['wick_ratio']:.2f}\n\n"
        f"<i>🎯 Короткий сильный импульс с ростом объёма ≥50%.</i>"
    )

# ========================= ОСНОВНОЙ ЦИКЛ =========================

def main():
    print("🚀 ЗАПУСК БОТА: ВСПЛЕСК ≥10% ЗА 5 МИНУТ С РОСТОМ ОБЪЁМА ≥50%")
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ Укажи TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID!")
        return

    exchange = ccxt.bybit({"enableRateLimit": True})
    recent_signals = {}
    signals_today = set()  # Сюда добавляем монеты, по которым уже были сигналы

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

    send_telegram("🤖 <b>Бот запущен</b>: фильтр всплески ≥10% / 5м, RSI ≥72, объём ≥50% роста. Цель ≤20 сигналов/день. Сигналы по монете не повторяются.")

    daily_signals = 0
    last_reset = time.time()

    while True:
        try:
            # Сброс счетчика и списка сигналов каждые 24 часа
            if time.time() - last_reset > 86400:
                daily_signals = 0
                last_reset = time.time()
                signals_today.clear()
                print("🔄 Сброс дневного счётчика и списка сигналов")

            print(f"\n⏱️ Сканирование... | Сегодня: {daily_signals}/{MAX_SIGNALS_PER_DAY}")

            for symbol in symbols:
                if daily_signals >= MAX_SIGNALS_PER_DAY:
                    print("🛑 Достигнут лимит сигналов")
                    break

                if symbol in signals_today:  # Пропускаем повторные сигналы по одной монете
                    continue

                try:
                    ohlcv = exchange.fetch_ohlcv(symbol, '5m', limit=10)
                    ticker = exchange.fetch_ticker(symbol)
                    if not ohlcv or len(ohlcv) < 5:
                        continue

                    signal = analyze_burst_signal(symbol, ohlcv, ticker)
                    if not signal:
                        continue

                    now = time.time()
                    if symbol in recent_signals and (now - recent_signals[symbol]) < SIGNAL_COOLDOWN_MIN * 60:
                        continue

                    # Добавляем монету в список сигналов на сегодня
                    recent_signals[symbol] = now
                    signals_today.add(symbol)
                    send_telegram(format_signal_message(signal))
                    daily_signals += 1
                    print(f"🎯 СИГНАЛ #{daily_signals}: {symbol}")

                except Exception as e:
                    print(f"Ошибка {symbol}: {e}")
                    continue

            # Очистка старых сигналов из recent_signals (кулдаун)
            now = time.time()
            recent_signals = {k: v for k, v in recent_signals.items() if now - v < SIGNAL_COOLDOWN_MIN * 60 * 2}

        except Exception as e:
            print(f"💥 Ошибка цикла: {e}")
            time.sleep(10)

        print(f"⏰ Следующий цикл через {POLL_INTERVAL_SEC} сек...")
        time.sleep(POLL_INTERVAL_SEC)

if __name__ == "__main__":
    main()
