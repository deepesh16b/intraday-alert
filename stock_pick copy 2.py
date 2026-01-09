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

UPSTOX_ACCESS_TOKEN = 'eyJ0eXAiOiJKV1QiLCJrZXlfaWQiOiJza192MS4wIiwiYWxnIjoiSFMyNTYifQ.eyJzdWIiOiIzWUNRQ0wiLCJqdGkiOiI2OTNjNWNkNDgwM2NjMDFlMjY2OTJmNTMiLCJpc011bHRpQ2xpZW50IjpmYWxzZSwiaXNQbHVzUGxhbiI6ZmFsc2UsImlhdCI6MTc2NTU2MzYwNCwiaXNzIjoidWRhcGktZ2F0ZXdheS1zZXJ2aWNlIiwiZXhwIjoxNzY1NTc2ODAwfQ.wX9AEIN1zOo9dftWNE0P8iuGu8lB01a3B16fOC3zGpM'
TELEGRAM_BOT_TOKEN = '8506967024:AAHhyrhtNkHh5l6NrEhmp6YtFbZBIv4c_Hs'
TELEGRAM_CHAT_ID = '1249217357'

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

def add_indicators_and_save(
    input_csv="daily_candles.csv",
    output_csv="daily_candles_with_indicators.csv"
):
    logger.info("🧮 Adding indicators: 44SMA, RSI14, 20D Volume Avg")

    df = pd.read_csv(input_csv)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df.sort_values(['symbol', 'timestamp'], inplace=True)

    result_frames = []

    for symbol, g in df.groupby('symbol', sort=False):
        g = g.copy()
        g.sort_values('timestamp', inplace=True)

        # ===== 44 SMA (Close) =====
        g['sma_44'] = g['close'].rolling(window=44, min_periods=44).mean()

        # ===== 20 Day Volume Average =====
        g['vol_avg_20'] = g['volume'].rolling(window=20, min_periods=20).mean()

        # ===== RSI 14 (Wilder Method) =====
        delta = g['close'].diff()

        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)

        avg_gain = gain.rolling(window=14, min_periods=14).mean()
        avg_loss = loss.rolling(window=14, min_periods=14).mean()

        rs = avg_gain / avg_loss
        g['rsi_14'] = 100 - (100 / (1 + rs))

        result_frames.append(g)

        logger.info(f"✅ Indicators added for {symbol}")

    final_df = pd.concat(result_frames)
    final_df.sort_values(['symbol', 'timestamp'], inplace=True)
    final_df.to_csv(output_csv, index=False)

    logger.info(f"📁 Saved indicator data → {output_csv}")


def detect_swing_trades(symbol: str, inst_key: str, df: pd.DataFrame) -> list:
    trades = []

    df = df.copy()
    df.sort_index(inplace=True)

    # We already have indicators in CSV
    required_cols = ['sma_44', 'rsi_14', 'vol_avg_20']
    if not all(col in df.columns for col in required_cols):
        logger.warning(f"{symbol}: Missing indicators, skipping")
        return trades

    df.reset_index(inplace=True)

    for i in range(50, len(df) - 2):  # start from 50th candle
        row = df.loc[i]
        row_prev = df.loc[i-1]
        # skip if indicators not available
        if pd.isna(row['sma_44']) or pd.isna(row['rsi_14']) or pd.isna(row['vol_avg_20']):
            continue

        # ---------- A) 44 SMA TREND CHECK ----------
        try:
            sma_today = row['sma_44']
            sma_3_back = df.loc[i - 3, 'sma_44']
            sma_6_back = df.loc[i - 6, 'sma_44']
        except KeyError:
            continue

        if not (sma_today > sma_3_back and sma_today > sma_6_back):
            continue

        # ---------- B) RSI + VOLUME ----------
        if not (38 <= row['rsi_14'] <= 58):
            continue

        if row['volume'] <= row['vol_avg_20']:
            continue

        # Case 1: row enters 44ma zone and next green candel closing appears above zone
        if not (
            row_prev['open'] > row_prev['close'] and 
            row_prev['low'] <= row_prev['sma_44']*(1 + SUPPORT_TOLERANCE) and
            row['open'] < row['close'] and
            row['close'] > row['sma_44']*(1 + SUPPORT_TOLERANCE)
        ):
            continue

        # Case 2: Graph goes down the 44sma and a green candel goes above 44sma
        if not(
            row['open'] < row['close'] and
            row['low'] < row['sma_44'] *(1 + SUPPORT_TOLERANCE) and
            row['close'] > row['sma_44'] *(1 + SUPPORT_TOLERANCE)
        ):
            continue

        # ---------- E) TRADE ----------
        entry_row = df.loc[i + 1]
        signal_row = df.loc[i]
        entryprice = round(entry_row['open'], 2)
        SL = round(max(min(signal_row['sma_44'], signal_row['open']),entry_row['open']*0.98 ), 2)
        trade = {
            'symbol': symbol,
            'instrument_key': inst_key,
            'support_date': df.loc[i, 'timestamp'].date().isoformat(),
            'signal_date': signal_row['timestamp'].date().isoformat(),
            'entry_date': entry_row['timestamp'].date().isoformat(),
            'entry_price': entryprice,
            'stop_loss': SL,
            'stop_loss_percent' :  round((entryprice-SL)/entryprice*100,2)
        }

        trades.append(trade)

    logger.info(f"🔍 {symbol}: found {len(trades)} trades")
    return trades

def main():
    symbols = pd.read_csv(SYMBOLS_CSV)

    # -------- FIRST LOOP: FETCH & SAVE ALL CANDLES --------
    daily_data_list = []

    for idx, row in symbols.iterrows():
        symbol = row.get('tradingsymbol') or row.get('symbol')
        key = row['instrument_key']
        logger.info(f"[1] Fetching candles for {symbol} ({idx+1}/{len(symbols)})")

        df_daily = fetch_and_cache_daily(key)
        df_daily['symbol'] = symbol
        df_daily['instrument_key'] = key

        daily_data_list.append(df_daily)

        time.sleep(0.2)

    # Save all candles into a single CSV
    if daily_data_list:
        df_all = pd.concat(daily_data_list)
        df_all.to_csv("daily_candles.csv")
        logger.info("📁 Saved all candle data to daily_candles.csv")

    # -------- FEATURE ENGINEERING STEP --------
    add_indicators_and_save(
        input_csv="daily_candles.csv",
        output_csv="daily_candles_with_indicators.csv"
    )

    # -------- SECOND LOOP: DETECT TRADES USING SAVED DATA --------
    df_all = pd.read_csv("daily_candles_with_indicators.csv")

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
