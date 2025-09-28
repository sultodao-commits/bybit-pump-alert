#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import asyncio
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import ccxt
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ===================== КОНФИГ =====================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "").strip()
assert BOT_TOKEN and CHAT_ID, "Нужно указать TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID"

POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", "60"))   # период сканирования рынка
LOOKAHEAD_MIN     = int(os.getenv("LOOKAHEAD_MIN", "15"))       # окно для вероятности
PROB_SAMPLE_N     = int(os.getenv("PROB_SAMPLE_N", "20"))       # сколько последних кейсов брать

# Пороги (указываются как положительные числа!)
THRESH_5M_PCT       = float(os.getenv("THRESH_5M_PCT", "6"))
THRESH_15M_PCT      = float(os.getenv("THRESH_15M_PCT", "12"))
THRESH_5M_DROP_PCT  = float(os.getenv("THRESH_5M_DROP_PCT", "6"))
THRESH_15M_DROP_PCT = float(os.getenv("THRESH_15M_DROP_PCT", "12"))

# Фильтры ликвидности
MIN_24H_QUOTE_VOLUME_USDT = float(os.getenv("MIN_24H_QUOTE_VOLUME_USDT", "500000"))
MIN_LAST_PRICE_USDT       = float(os.getenv("MIN_LAST_PRICE_USDT", "0.002"))

# Ежедневный отчёт
DAILY_REPORT_HOUR_UTC = int(os.getenv("DAILY_REPORT_HOUR_UTC", "6"))

DB_PATH = os.path.join(os.path.dirname(__file__), "state.db")

# ===================== УТИЛИТЫ TG =====================

def tg_send(text: str, chat_id: Optional[str] = None) -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        requests.post(
            url,
            json={
                "chat_id": chat_id or CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
    except Exception as e:
        print("[TG]", e)

# ===================== БАЗА ДАННЫХ =====================

def db() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)

def db_init() -> None:
    con = db(); c = con.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,     -- 'pump' | 'dump'
            timeframe TEXT NOT NULL,     -- '5m' | '15m'
            percent REAL NOT NULL,
            price REAL NOT NULL,         -- close на момент сигнала
            ts TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    con.commit(); con.close()

def save_alert(symbol: str, direction: str, timeframe: str, percent: float, price: float) -> None:
    con = db(); c = con.cursor()
    c.execute(
        "INSERT INTO alerts(symbol, direction, timeframe, percent, price) VALUES (?,?,?,?,?)",
        (symbol, direction, timeframe, percent, price),
    )
    con.commit(); con.close()

def last_n_alerts(symbol: str, direction: str, limit_n: int) -> List[Tuple[str, float, float]]:
    con = db(); c = con.cursor()
    c.execute("""
        SELECT ts, percent, price
        FROM alerts
        WHERE symbol=? AND direction=?
        ORDER BY ts DESC
        LIMIT ?
    """, (symbol, direction, limit_n))
    rows = c.fetchall()
    con.close()
    return rows

def simple_history_stats(symbol: str, direction: str, limit_n: int = 10) -> Optional[Dict[str, float]]:
    rows = last_n_alerts(symbol, direction, limit_n)
    if not rows:
        return None
    vals = [float(r[1]) for r in rows]
    return {
        "count": len(vals),
        "avg": sum(vals)/len(vals),
        "max": max(vals),
        "min": min(vals),
    }

def meta_get(key: str) -> Optional[str]:
    con = db(); c = con.cursor()
    c.execute("SELECT value FROM meta WHERE key=?", (key,))
    row = c.fetchone()
    con.close()
    return row[0] if row else None

def meta_set(key: str, value: str) -> None:
    con = db(); c = con.cursor()
    c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)", (key, value))
    con.commit(); con.close()

# ===================== BYBIT FUTURES (ccxt) =====================

def ex_futures() -> ccxt.bybit:
    # Для перпетуалов Bybit у ccxt корректно указывать defaultType="swap"
    return ccxt.bybit({
        "enableRateLimit": True,
        "timeout": 20000,
        "options": {"defaultType": "swap"},
    })

def pick_swap_usdt_symbols(ex: ccxt.bybit,
                           min_qv_usdt: float,
                           min_last_price: float) -> List[str]:
    markets = ex.load_markets(reload=True)
    tickers = ex.fetch_tickers(params={"type": "swap"})
    out: List[str] = []
    for sym, m in markets.items():
        try:
            if m.get("type") != "swap" or not m.get("swap"):
                continue
            if not m.get("linear"):
                continue
            if m.get("settle") != "USDT":
                continue
            if m.get("quote") != "USDT":
                continue
            base = m.get("base", "")
            if any(tag in base for tag in ["UP", "DOWN", "3L", "3S", "4L", "4S"]):
                continue
            t = tickers.get(sym, {})
            qv = float(t.get("quoteVolume") or t.get("baseVolume") or 0.0)
            last = float(t.get("last") or t.get("close") or 0.0)
            if qv < min_qv_usdt or last < min_last_price:
                continue
            out.append(sym)
        except Exception:
            continue
    return out

# ===================== РАСЧЁТ ВЕРОЯТНОСТИ И ДИНАМИКИ =====================

def probability_after(ex: ccxt.bybit, symbol: str, direction: str,
                      lookahead_min: int = LOOKAHEAD_MIN, sample_n: int = PROB_SAMPLE_N) -> Tuple[Optional[float], int]:
    """
    Вероятность через lookahead_min:
      pump -> доля случаев, когда close позже < входной цены (откат)
      dump -> доля случаев, когда close позже > входной цены (отскок)
    """
    rows = last_n_alerts(symbol, direction, sample_n)
    if not rows:
        return None, 0

    succ, tot = 0, 0
    for ts_str, _, entry_price in rows:
        try:
            since_ms = int(datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").timestamp()) * 1000
            ohl = ex.fetch_ohlcv(symbol, "1m", since=since_ms, limit=lookahead_min + 3)
            if len(ohl) < lookahead_min:
                continue
            future_close = float(ohl[-1][4])
            if direction == "pump" and future_close < float(entry_price):
                succ += 1
            if direction == "dump" and future_close > float(entry_price):
                succ += 1
            tot += 1
        except Exception:
            continue
    if tot == 0:
        return None, 0
    return round(succ / tot * 100.0, 1), tot

def extended_dynamics(ex: ccxt.bybit, symbol: str, direction: str,
                      sample_n: int = PROB_SAMPLE_N) -> Dict[int, Optional[float]]:
    """
    Средние изменения через 5/15/30/60 минут относительно цены входа.
    """
    rows = last_n_alerts(symbol, direction, sample_n)
    horizons = [5, 15, 30, 60]
    store: Dict[int, List[float]] = {h: [] for h in horizons}
    if not rows:
        return {h: None for h in horizons}

    for ts_str, _, entry_price in rows:
        try:
            since_ms = int(datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").timestamp()) * 1000
            ohl = ex.fetch_ohlcv(symbol, "1m", since=since_ms, limit=65)
            closes = [float(x[4]) for x in ohl]
            if not closes:
                continue
            for h in horizons:
                idx = min(h, len(closes)-1)
                chg = (closes[idx] / float(entry_price) - 1.0) * 100.0
                store[h].append(chg)
        except Exception:
            continue
    return {h: (round(sum(v)/len(v), 2) if v else None) for h, v in store.items()}

# ===================== СКАН РЫНКА =====================

def scan_symbols(ex: ccxt.bybit, symbols: List[str]) -> None:
    for sym in symbols:
        try:
            ohlcv = ex.fetch_ohlcv(sym, "1m", limit=16)
            if len(ohlcv) < 16:
                continue
            c_now  = float(ohlcv[-1][4])
            c_5m   = float(ohlcv[-6][4])
            c_15m  = float(ohlcv[-16][4])
            chg_5m  = (c_now / c_5m  - 1.0) * 100.0
            chg_15m = (c_now / c_15m - 1.0) * 100.0

            # Пампы
            if chg_5m  >= THRESH_5M_PCT:   fire_alert(ex, sym, "pump", "5m",  chg_5m,  c_now)
            if chg_15m >= THRESH_15M_PCT:  fire_alert(ex, sym, "pump", "15m", chg_15m, c_now)
            # Дампы
            if chg_5m  <= -THRESH_5M_DROP_PCT:   fire_alert(ex, sym, "dump", "5m",  chg_5m,  c_now)
            if chg_15m <= -THRESH_15M_DROP_PCT:  fire_alert(ex, sym, "dump", "15m", chg_15m, c_now)

        except Exception:
            # тихо пропускаем редкие ошибки по конкретному символу, чтобы не засорять логи
            continue

def fire_alert(ex: ccxt.bybit, symbol: str, direction: str, timeframe: str, percent: float, price: float) -> None:
    save_alert(symbol, direction, timeframe, percent, price)
    stats = simple_history_stats(symbol, direction, 10)
    prob, tot = probability_after(ex, symbol, direction)
    dyn = extended_dynamics(ex, symbol, direction)

    icon = "🚨 Памп" if direction == "pump" else "🔻 Дамп"
    msg = f"{icon} {timeframe}\nКонтракт: <b>{symbol}</b>\nИзменение: <b>{percent:.2f}%</b> за {timeframe}"

    if stats:
        msg += (f"\n\n📊 История ({stats['count']} последних {direction}ов):"
                f"\n— Среднее изменение: <b>{stats['avg']:.2f}%</b>"
                f"\n— Макс: <b>{stats['max']:.2f}%</b>, Мин: <b>{stats['min']:.2f}%</b>")

    if prob is not None:
        if direction == "pump":
            msg += f"\n\n🎯 Вероятность падения через {LOOKAHEAD_MIN}м: <b>{prob}%</b> (по {tot} случаям)"
        else:
            msg += f"\n\n🎯 Вероятность отскока через {LOOKAHEAD_MIN}м: <b>{prob}%</b> (по {tot} случаям)"

    if dyn:
        msg += "\n⏱ Среднее поведение:"
        for h in [5, 15, 30, 60]:
            val = dyn.get(h)
            if val is not None:
                msg += f"\n   {h}м: <b>{val:.2f}%</b>"

    msg += "\n\n<i>Не финсовет. Риски на вас.</i>"
    tg_send(msg)

# ===================== ДНЕВНОЙ ОТЧЁТ =====================

def maybe_daily_report() -> None:
    try:
        utc = datetime.utcnow()
        if utc.hour != DAILY_REPORT_HOUR_UTC:
            return
        today = utc.strftime("%Y-%m-%d")
        if meta_get("daily_report_date") == today:
            return
        con = db(); c = con.cursor()
        c.execute("SELECT direction, COUNT(*) FROM alerts WHERE ts>=datetime('now','-24 hours') GROUP BY direction")
        rows = c.fetchall(); con.close()
        pumps = next((cnt for d, cnt in rows if d == "pump"), 0)
        dumps = next((cnt for d, cnt in rows if d == "dump"), 0)
        tg_send(f"📅 Дневной отчёт (24ч)\n— Пампов: <b>{pumps}</b>\n— Дампов: <b>{dumps}</b>\n— UTC: {utc.strftime('%Y-%m-%d %H:%M')}")
        meta_set("daily_report_date", today)
    except Exception as e:
        print("[REPORT]", e)

# ===================== КОМАНДЫ TELEGRAM =====================

async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    con = db(); c = con.cursor()
    c.execute("SELECT symbol, direction, COUNT(*) FROM alerts WHERE ts>=datetime('now','-24 hours') GROUP BY symbol, direction")
    rows = c.fetchall(); con.close()
    if not rows:
        await update.message.reply_text("Нет сигналов за 24ч."); return
    msg = "📈 Отчёт за 24ч:\n"
    for sym, dir_, cnt in rows:
        icon = "🚨" if dir_ == "pump" else "🔻"
        msg += f"{icon} {sym}: {cnt} ({dir_})\n"
    await update.message.reply_text(msg)

async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    con = db(); c = con.cursor()
    c.execute("SELECT symbol, COUNT(*) AS cnt FROM alerts GROUP BY symbol ORDER BY cnt DESC LIMIT 5")
    rows = c.fetchall(); con.close()
    if not rows:
        await update.message.reply_text("Данных пока нет."); return
    msg = "🔥 Топ-5 по числу сигналов:\n"
    for sym, cnt in rows:
        msg += f"{sym}: {cnt}\n"
    await update.message.reply_text(msg)

async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /history SYMBOL"); return
    symbol = context.args[0].upper()
    con = db(); c = con.cursor()
    c.execute("SELECT ts, direction, timeframe, percent FROM alerts WHERE symbol=? ORDER BY ts DESC LIMIT 10", (symbol,))
    rows = c.fetchall(); con.close()
    if not rows:
        await update.message.reply_text(f"Нет истории по {symbol}."); return
    msg = f"📜 История {symbol} (посл. {len(rows)}):"
    for ts, d, tf, pct in rows:
        icon = "🚨" if d == "pump" else "🔻"
        msg += f"\n{ts} {icon} {tf}: {pct:.2f}%"
    await update.message.reply_text(msg)

# ===================== MAIN =====================

async def market_loop(symbols: List[str]) -> None:
    ex = ex_futures()
    while True:
        try:
            scan_symbols(ex, symbols)
            maybe_daily_report()
        except Exception as e:
            print("[LOOP]", e)
        await asyncio.sleep(POLL_INTERVAL_SEC)

def main() -> None:
    db_init()
    tg_send("✅ Бот запущен (Bybit Futures; пампы+дампы; вероятность; динамика; команды).")

    ex = ex_futures()
    try:
        symbols = pick_swap_usdt_symbols(ex, MIN_24H_QUOTE_VOLUME_USDT, MIN_LAST_PRICE_USDT)
        tg_send(f"📊 К мониторингу фьючерсных контрактов: <b>{len(symbols)}</b>")
    except Exception as e:
        print("[SYMBOLS]", e)
        symbols = []

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("report",  cmd_report))
    app.add_handler(CommandHandler("top",     cmd_top))
    app.add_handler(CommandHandler("history", cmd_history))

    # Фоновая задача сканирования рынка
    app.job_queue.run_once(lambda *_: None, when=0)  # инициализация JobQueue (фиктивный вызов)
    asyncio.get_event_loop().create_task(market_loop(symbols))

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
