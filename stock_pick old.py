import os
import datetime
import time
import logging
import requests
import pandas as pd
import numpy as np

# ======= CONFIGURATION ========
SYMBOLS_CSV        = 'symbols_data.csv'
OUTPUT_CSV         = 'swing_trades.csv'
CACHE_DIR          = 'cache_daily'

START_DATE         = datetime.date(2015, 1, 1)
END_DATE           = datetime.date(2025, 1, 1)
MA_PERIOD_44       = 44
MA_PERIOD_20       = 20
SUPPORT_TOLERANCE  = 0.001
MAX_TOUCH_DAYS     = 1

API_BASE_URL       = 'https://api.upstox.com/v3/historical-candle'
HEADERS            = {'Content-Type': 'application/json',
                        'Accept': 'application/json',
                        'Authorization': 'Bearer {eyJ0eXAiOiJKV1QiLCJrZXlfaWQiOiJza192MS4wIiwiYWxnIjoiSFMyNTYifQ.eyJzdWIiOiIzWUNRQ0wiLCJqdGkiOiI2OTNjNWNkNDgwM2NjMDFlMjY2OTJmNTMiLCJpc011bHRpQ2xpZW50IjpmYWxzZSwiaXNQbHVzUGxhbiI6ZmFsc2UsImlhdCI6MTc2NTU2MzYwNCwiaXNzIjoidWRhcGktZ2F0ZXdheS1zZXJ2aWNlIiwiZXhwIjoxNzY1NTc2ODAwfQ.wX9AEIN1zOo9dftWNE0P8iuGu8lB01a3B16fOC3zGpM}'
                     }

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

def ensure_cache():
    os.makedirs(CACHE_DIR, exist_ok=True)

def sanitize_key(key: str) -> str:
    return key.replace('|', '_')

def fetch_and_cache_daily(inst_key: str) -> pd.DataFrame:
    ensure_cache()
    sd = START_DATE.strftime('%Y-%m-%d')
    ed = END_DATE.strftime('%Y-%m-%d')
    safe_key = sanitize_key(inst_key)
    cache_file = os.path.join(CACHE_DIR, f"{safe_key}_{sd}_{ed}.pkl")

    if os.path.exists(cache_file):
        logger.info(f"💾 Loaded cache for {inst_key}")
        df_cached = pd.read_pickle(cache_file)
        df_cached.sort_index(inplace=True)
        return df_cached

    url = f"{API_BASE_URL}/{inst_key}/days/1/{ed}/{sd}"
    logger.info(f"🔄 Fetching daily for {inst_key}: {sd} → {ed}")
    resp = requests.get(url, headers=HEADERS)
    resp.raise_for_status()
    data = resp.json().get('data', {}).get('candles', [])
    df = pd.DataFrame(data, columns=[
        'timestamp','open','high','low','close','volume','open_interest'
    ])
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df.set_index('timestamp', inplace=True)
    df = df.tz_localize(None)
    df.sort_index(inplace=True)
    df.to_pickle(cache_file)
    logger.info(f"✅ Cached {len(df)} bars for {inst_key}")
    return df

def detect_swing_trades(symbol: str, inst_key: str, df: pd.DataFrame) -> list:
    df = df.copy()
    # 44 days direction matters in csv, past or future!
    df['ma44'] = df['close'].rolling(MA_PERIOD_44).mean()
    df['ma20'] = df['close'].rolling(MA_PERIOD_20).mean()
    df.dropna(inplace=True)
    trades = [] 

    # Start after both MA values are available
    for i in range(max(MA_PERIOD_44, MA_PERIOD_20) + MAX_TOUCH_DAYS, len(df) - 1):
        today = df.iloc[i]
        prev = df.iloc[i - 1]

        # 1) Check MA condition: 20MA > 44MA and both rising
        if not (
            today["close"] > today["open"] and
            today["ma20"] > today["ma44"] and
            df["ma20"].iloc[i] > df["ma20"].iloc[i-6] and
            df["ma44"].iloc[i] > df["ma44"].iloc[i-6]
        ):
            continue

        # 2) Check recent touches to 44MA
        touched = False
        for j in range(i - MAX_TOUCH_DAYS, i):
            cand = df.iloc[j]
            if cand['low'] <= cand['ma44'] * (1 + SUPPORT_TOLERANCE) and cand['high'] >= cand['ma44'] * (1 - SUPPORT_TOLERANCE):
                touched = True
                break
        if not touched:
            continue

        # 3) Current candle bullish and closes above 44MA
        body = today['close'] - today['open']
        if body <= 0 or today['close'] <= today['ma44']:
            continue

        # 4) Next day breakout above today’s high
        nxt = df.iloc[i + 1]
        if nxt['high'] <= today['high'] * 1.005:
            continue

        # Trade parameters
        entry_price = round(today['high'] * 1.005, 2)
        sl = min(prev['low'], today['low'])
        risk = entry_price - sl
        target = entry_price + 2.5 * risk

        trades.append({
            'symbol': symbol,
            'instrument_key': inst_key,
            'signal_date': today.name.date().isoformat(),
            'entry_date': nxt.name.date().isoformat(),
            'entry_price': round(entry_price, 2),
            'stop_loss': round(sl, 2),
            'target': round(target, 2)
        })

    logger.info(f"🔍 {symbol}: found {len(trades)} swing trades")
    return trades

def main():
    symbols = pd.read_csv(SYMBOLS_CSV)

    # -------- FIRST LOOP: FETCH & SAVE ALL CANDLES --------
    # daily_data_list = []

    # for idx, row in symbols.iterrows():
    #     symbol = row.get('tradingsymbol') or row.get('symbol')
    #     key = row['instrument_key']
    #     logger.info(f"[1] Fetching candles for {symbol} ({idx+1}/{len(symbols)})")

    #     df_daily = fetch_and_cache_daily(key)
    #     df_daily['symbol'] = symbol
    #     df_daily['instrument_key'] = key

    #     daily_data_list.append(df_daily)

    #     time.sleep(0.2)

    # # Save all candles into a single CSV
    # if daily_data_list:
    #     df_all = pd.concat(daily_data_list)
    #     df_all.to_csv("daily_candles.csv")
    #     logger.info("📁 Saved all candle data to daily_candles.csv")

    # -------- SECOND LOOP: DETECT TRADES USING SAVED DATA --------
    df_all = pd.read_csv("daily_candles.csv")
    df_all['timestamp'] = pd.to_datetime(df_all['timestamp'])
    df_all.set_index('timestamp', inplace=True)

    all_trades = []

    for idx, row in symbols.iterrows():
        symbol = row.get('tradingsymbol') or row.get('symbol')
        key = row['instrument_key']

        logger.info(f"[2] Detecting trades for {symbol} ({idx+1}/{len(symbols)})")

        df_symbol = df_all[df_all['instrument_key'] == key].copy()
        df_symbol.sort_index(inplace=True)

        trades = detect_swing_trades(symbol, key, df_symbol)
        all_trades.extend(trades)

    # Save results
    if all_trades:
        pd.DataFrame(all_trades).sort_values(['symbol', 'entry_date']).to_csv(
            OUTPUT_CSV, index=False
        )
        logger.info(f"🎉 Saved {len(all_trades)} trades to {OUTPUT_CSV}")
    else:
        logger.info("😞 No trades found.")


if __name__ == '__main__':
    main()
