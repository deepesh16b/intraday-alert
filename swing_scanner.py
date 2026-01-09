import os
import datetime
import time
import requests
import pandas as pd
import numpy as np

# ======= CONFIGURATION FROM ENV (Set these in GitHub Secrets) ========
UPSTOX_ACCESS_TOKEN = os.environ.get('UPSTOX_ACCESS_TOKEN') 
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

# CSV containing your list of stocks (must have 'instrument_key' and 'tradingsymbol')
SYMBOLS_CSV = 'symbols_data.csv'

# Strategy Settings
MA_PERIOD_44 = 44
RSI_PERIOD = 14
SUPPORT_TOLERANCE = 0.002 # 0.2% tolerance

API_BASE_URL = 'https://api.upstox.com/v3/historical-candle'

def send_telegram_msg(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram keys not found. Skipping message.")
        print(message)
        return
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, json=payload)
        print("✅ Telegram sent!")
    except Exception as e:
        print(f"❌ Failed to send Telegram: {e}")

def fetch_data(inst_key):
    # Fetch last 100 days (enough for 44MA)
    end_date = datetime.date.today()
    start_date = end_date - datetime.timedelta(days=150) # Buffer for holidays
    inst_key = inst_key.replace("|", "%7C")
    url = f"{API_BASE_URL}/{inst_key}/days/1/{end_date}/{start_date}"
    headers = {
        'Accept': 'application/json',
        'Authorization': f'Bearer {UPSTOX_ACCESS_TOKEN}'
    }
    
    try:
        resp = requests.get(url, headers=headers)
        if resp.status_code != 200:
            return None
        data = resp.json().get('data', {}).get('candles', [])
        if not data:
            return None
            
        df = pd.DataFrame(data, columns=['timestamp','open','high','low','close','volume','oi'])
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        df = df.sort_values('timestamp').reset_index(drop=True)
        return df
    except Exception as e:
        print(f"Error fetching {inst_key}: {e}")
        return None

def calculate_indicators(df):
    df['sma_44'] = df['close'].rolling(window=44).mean()
    df['vol_avg_20'] = df['volume'].rolling(window=20).mean()
    
    # RSI
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['rsi_14'] = 100 - (100 / (1 + rs))
    return df

def check_signal(df, symbol):
    if len(df) < 50: return None
    
    # Get the last candle (Today) and previous (Yesterday)
    # NOTE: If running after market close, -1 is Today.
    row = df.iloc[-1]      # Today
    prev = df.iloc[-2]     # Yesterday
    
    # 1. Indicator Checks
    if pd.isna(row['sma_44']) or pd.isna(row['rsi_14']): return None
    
    # RSI Filter (38-58)
    if not (38 <= row['rsi_14'] <= 60): return None # Expanded slightly to 60
    
    # Volume Filter
    if row['volume'] <= row['vol_avg_20']: return None
    
    # Trend Check (SMA rising compared to 5 days ago)
    sma_5_back = df.iloc[-6]['sma_44']
    if row['sma_44'] <= sma_5_back: return None

    # 2. Strategy Logic (Bounce off 44MA)
    
    # Logic A: Green candle today, Low touched 44MA zone, Close above 44MA
    is_green = row['close'] > row['open']
    touched_sma = row['low'] <= row['sma_44'] * (1 + SUPPORT_TOLERANCE)
    closed_above = row['close'] > row['sma_44']
    
    signal_found = False
    
    if is_green and touched_sma and closed_above:
        signal_found = True
        
    if signal_found:
        entry_price = row['high'] # Buy above today's high
        sl = min(row['low'], row['sma_44'])
        target = entry_price + (entry_price - sl) * 2 # 1:2 Risk Reward
        
        return {
            'Symbol': symbol,
            'Date': row['timestamp'].strftime('%Y-%m-%d'),
            'Price': row['close'],
            'Entry (Above)': entry_price,
            'SL': sl,
            'Target': target,
            'RSI': round(row['rsi_14'], 2)
        }
    return None

def main():
    if not os.path.exists(SYMBOLS_CSV):
        print(f"❌ {SYMBOLS_CSV} not found!")
        return

    symbols = pd.read_csv(SYMBOLS_CSV)
    print(f"🚀 Starting Scan for {len(symbols)} symbols...")
    
    valid_trades = []
    
    for idx, row in symbols.iterrows():
        symbol = row.get('tradingsymbol') or row.get('symbol')
        key = row['instrument_key']
        
        df = fetch_data(key)
        if df is not None:
            df = calculate_indicators(df)
            signal = check_signal(df, symbol)
            if signal:
                print(f"🔔 SIGNAL FOUND: {symbol}")
                valid_trades.append(signal)
        
        time.sleep(0.1) # Be nice to API

    # Prepare Report
    if valid_trades:
        msg = f"📢 **SWING TRADE SIGNALS ({datetime.date.today()})**\n\n"
        for t in valid_trades:
            msg += f"🚀 *{t['Symbol']}*\n"
            msg += f"   Entry > {t['Entry (Above)']}\n"
            msg += f"   SL: {t['SL']} | Tgt: {t['Target']}\n"
            msg += f"   RSI: {t['RSI']}\n\n"
        
        print(msg)
        send_telegram_msg(msg)
    else:
        print("No trades found today.")
        send_telegram_msg(f"📉 Scan Complete ({datetime.date.today()}): No signals found.")

if __name__ == '__main__':
    main()