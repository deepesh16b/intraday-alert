import datetime
import time
import os
import requests
import pandas as pd
import numpy as np
import math
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import Rectangle
from matplotlib.ticker import FuncFormatter


# ================= ENV CONFIG =================

UPSTOX_ACCESS_TOKEN = 'eyJ0eXAiOiJKV1QiLCJrZXlfaWQiOiJza192MS4wIiwiYWxnIjoiSFMyNTYifQ.eyJzdWIiOiIzWUNRQ0wiLCJqdGkiOiI2OTNjNWNkNDgwM2NjMDFlMjY2OTJmNTMiLCJpc011bHRpQ2xpZW50IjpmYWxzZSwiaXNQbHVzUGxhbiI6ZmFsc2UsImlhdCI6MTc2NTU2MzYwNCwiaXNzIjoidWRhcGktZ2F0ZXdheS1zZXJ2aWNlIiwiZXhwIjoxNzY1NTc2ODAwfQ.wX9AEIN1zOo9dftWNE0P8iuGu8lB01a3B16fOC3zGpM'
TELEGRAM_BOT_TOKEN = '8506967024:AAHhyrhtNkHh5l6NrEhmp6YtFbZBIv4c_Hs'
TELEGRAM_CHAT_ID = '1249217357'
SYMBOLS_CSV = "symbols_data.csv"

MA_PERIOD_44 = 44
RSI_PERIOD = 14
SUPPORT_TOLERANCE = 0.002

API_BASE_URL = "https://api.upstox.com/v3/historical-candle"

MAX_SIGNALS = 10          # 🔥 hard limit
CANDLES_TO_PLOT = 100     # 🔥 keep light for GitHub Actions
API_SLEEP = 0.15          # 🔥 safe + fast

DARK_BG = "#131722"
GRID_COLOR = "#2A2E39"
TEXT_COLOR = "#A9B1BD"

DEBUG = True  # 🔥 set False later

if DEBUG== True:
    SUPPORT_TOLERANCE = 0.2
def log(msg):
    if DEBUG:
        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}")

def safe_get(url, headers, retries=3, timeout=15):
    for attempt in range(1, retries + 1):
        try:
            return requests.get(url, headers=headers, timeout=timeout)
        except requests.exceptions.RequestException as e:
            log(f"Network error (attempt {attempt}/{retries}): {e}")
            time.sleep(1.5 * attempt)  # simple backoff
    return None

# ================= TELEGRAM =================
def send_telegram_photo(image_path, caption):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    with open(image_path, "rb") as img:
        requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
            files={"photo": img},
            timeout=20
        )

# ================= DATA FETCH =================
def fetch_today_candle(inst_key):
    url = f"{API_BASE_URL}/intraday/{inst_key}/days/1"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {UPSTOX_ACCESS_TOKEN}"
    }
    r = safe_get(url, headers)
    if r is None:
        return None

    if r.status_code != 200:
        return None

    data = r.json().get("data", {}).get("candles", [])
    if not data:
        return None

    df = pd.DataFrame(
        data,
        columns=["timestamp","open","high","low","close","volume","oi"]
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df.iloc[[-1]]

def fetch_data(inst_key):
    end_date = datetime.date.today() - datetime.timedelta(days=1)
    start_date = end_date - datetime.timedelta(days=150)

    url = f"{API_BASE_URL}/{inst_key}/days/1/{end_date}/{start_date}"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {UPSTOX_ACCESS_TOKEN}"
    }

    r = safe_get(url, headers)
    if r is None:
        return None

    if r.status_code != 200:
        return None

    data = r.json().get("data", {}).get("candles", [])
    if not data:
        return None

    hist_df = pd.DataFrame(
        data,
        columns=["timestamp","open","high","low","close","volume","oi"]
    )
    hist_df["timestamp"] = pd.to_datetime(hist_df["timestamp"])
    hist_df = hist_df.sort_values("timestamp").reset_index(drop=True)

    today = fetch_today_candle(inst_key)
    if today is not None:
        hist_df = pd.concat([hist_df, today], ignore_index=True)
    log(f"Last candle: {hist_df.iloc[-1]['timestamp'].date()}")
    return hist_df

# ================= INDICATORS =================
def calculate_indicators(df):
    df["sma_44"] = df["close"].rolling(44).mean()
    df["vol_avg_20"] = df["volume"].rolling(20).mean()

    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = -delta.where(delta < 0, 0).rolling(14).mean()
    rs = gain / loss
    df["rsi_14"] = 100 - (100 / (1 + rs))
    return df

# ================= SIGNAL (UNCHANGED LOGIC) =================
def check_signal(df, symbol):
    if len(df) < 50:
        return None

    row = df.iloc[-1]
    prev = df.iloc[-2]
    prev2 = df.iloc[-3]

    if pd.isna(row["sma_44"]) or pd.isna(row["rsi_14"]):
        return None

    if DEBUG==False and not (38 <= row["rsi_14"] <= 60):
        return None

    if DEBUG==False and row["volume"] <= row["vol_avg_20"]:
        return None

    # sma trend upward check
    # --- NEW: CALCULATE SLOPE ANGLE ---
    lookback = 5
    sma_now = row["sma_44"]
    sma_old = df.iloc[-lookback]["sma_44"]
    
    # We calculate the vertical rise as a percentage to normalize across stocks
    # Rise = (Current SMA - Old SMA) / Old SMA * 100
    # Run = number of candles (10)
    percentage_rise = ((sma_now - sma_old) / sma_old) * 100
    
    # Calculate angle in degrees
    # We multiply by a 'sensitivity' factor (e.g., 10) so 1-2 degrees is meaningful
    angle_rad = math.atan2(percentage_rise, lookback)
    angle_deg = math.degrees(angle_rad)
    
    # Adjust this threshold: 0.5 to 1.5 is usually a solid "visible" uptrend
    MIN_ANGLE = 2
    if DEBUG==True:
        MIN_ANGLE=0.0
    
    if angle_deg < MIN_ANGLE:
        # if DEBUG: log(f"{symbol} ignored: SMA Not Inclined (Angle: {angle_deg:.2f}°)")
        return None
    else:
        log(f"{symbol} is Uptrend: (Angle: {angle_deg:.2f}°)")
    # ----------------------------------
    # Case 1: green candle cuts 44sma and goes upwards
    is_green = row["close"] > row["open"]
    touched_sma = row["low"] <= row["sma_44"] * (1 + SUPPORT_TOLERANCE)
    closed_above = row["close"] > row["sma_44"]

    is_signal=False

    if (is_green and touched_sma and closed_above):
        is_signal=True

    # Case 2: any of prev 2 red candle's low is below 44sma and next green candle opens above 44sma
    prev_is_red = prev['close'] < prev['open']
    prev_is_touched_sma = prev['low'] <= prev['sma_44']*(1+SUPPORT_TOLERANCE)
    prev2_is_red = prev2['close'] < prev2['open']
    prev2_is_touched_sma = prev2['low'] <= prev2['sma_44']*(1+SUPPORT_TOLERANCE)
    green_is_above_prev_and_sma = row['close'] > prev['high'] and row['low'] > row['sma_44']*(1+SUPPORT_TOLERANCE)
    if(is_green and green_is_above_prev_and_sma and (prev_is_red and prev2_is_red) and (prev_is_touched_sma or prev2_is_touched_sma) ):
        is_signal = True

    # Both Cases failed
    if(is_signal==False):
        return None
    
    entry = row["high"]
    sl = max(min(row["low"], row["sma_44"]), entry * 0.98)
    target = entry + (entry - sl) * 2

    return {
        "Symbol": symbol,
        "Entry": round(entry, 2),
        "SL": round(sl, 2),
        "Target": round(target, 2),
        "RSI": round(row["rsi_14"], 2),
        "SLP": round(((entry - sl) / entry) * 100, 2),
        "LastDate": df.iloc[-1]['timestamp'].date()
    }

def get_temp_dir():
    if os.name == "nt":  # Windows
        path = os.path.join(os.getcwd(), "charts")
    else:               # Linux (GitHub Actions)
        path = "/tmp"

    os.makedirs(path, exist_ok=True)
    return path

def compute_y_ticks(y_min, y_max, target_divs=12):
    price_range = y_max - y_min
    raw_step = price_range / target_divs

    steps = [5, 10, 25, 50, 100, 200, 400]
    step = min(steps, key=lambda x: abs(x - raw_step))

    start = (y_min // step) * step
    end = ((y_max + step) // step) * step

    ticks = []
    val = start
    while val <= end:
        ticks.append(val)
        val += step

    return ticks, start, end

# ================= CHART (MODIFIED) =================
def generate_chart(df, symbol, signal):
    df_plot = df.tail(CANDLES_TO_PLOT).copy()
    df_plot["mdates"] = mdates.date2num(df_plot["timestamp"])

    fig, ax = plt.subplots(figsize=(10, 5))
    fig.patch.set_facecolor(DARK_BG)
    ax.set_facecolor(DARK_BG)

    candle_width = 0.6
    
    # ===== Candlesticks =====
    for _, row in df_plot.iterrows():
        color = "#26A69A" if row["close"] >= row["open"] else "#EF5350"
        ax.plot([row["mdates"], row["mdates"]], [row["low"], row["high"]], color=color, linewidth=1)
        rect = Rectangle(
            (row["mdates"] - candle_width / 2, min(row["open"], row["close"])),
            candle_width, abs(row["close"] - row["open"]),
            facecolor=color, edgecolor=color
        )
        ax.add_patch(rect)

    # ===== 44 SMA =====
    ax.plot(df_plot["mdates"], df_plot["sma_44"], color="#3179F5", linewidth=1.1)

    # ===== Level Definitions =====
    target_val = signal['Target']
    sl_val = signal['SL']
    last_close = df_plot.iloc[-1]["close"]
    last_sma = df_plot.iloc[-1]["sma_44"]
    
    # Define how far left the dotted lines go (e.g., last 15 candles)
    line_start_idx = max(0, len(df_plot) - 15)
    line_start_date = df_plot["mdates"].iloc[line_start_idx]
    line_end_date = df_plot["mdates"].iloc[-1]

    # ===== Thin Dotted Lines (NEW) =====
    line_style = dict(linestyle="--", linewidth=0.8, alpha=0.7)
    
    # Target (Yellow)
    ax.hlines(y=target_val, xmin=line_start_date, xmax=line_end_date, color="yellow", **line_style)
    # SL (Red)
    ax.hlines(y=sl_val, xmin=line_start_date, xmax=line_end_date, color="#EF5350", **line_style)
    # Current Price (Teal)
    ax.hlines(y=last_close, xmin=line_start_date, xmax=line_end_date, color="#26A69A", **line_style)

    # ===== X & Y Axis Setup =====
    ax.xaxis.set_major_locator(mdates.MonthLocator(bymonthday=[1, 15]))
    ax.xaxis.set_major_formatter(FuncFormatter(lambda x, p: mdates.num2date(x).strftime('%d %b')))
    ax.tick_params(axis="both", colors=TEXT_COLOR, labelsize=8)

    y_min = min(df_plot["low"].min(), sl_val) * 0.99
    y_max = max(df_plot["high"].max(), target_val) * 1.01
    y_ticks, y_start, y_end = compute_y_ticks(y_min, y_max)

    ax.set_ylim(y_start, y_end)
    ax.set_yticks(y_ticks)
    ax.yaxis.tick_right()
    ax.yaxis.set_label_position("right")

    # ===== Grid & Spines =====
    ax.grid(True, color=GRID_COLOR, alpha=0.3, linewidth=0.5)
    for spine in ax.spines.values(): spine.set_color(GRID_COLOR)

    # ===== Right Side Labels (Fixed Overlap/Visibility) =====
    # We use a small transform to ensure labels stay on the right edge
    label_x = 1.005 
    
    # SMA (Blue)
    ax.text(label_x, last_sma, f"{last_sma:.2f}", transform=ax.get_yaxis_transform(),
            va="center", fontsize=7, color="white",
            bbox=dict(boxstyle="round,pad=0.2", fc="#3179F5", ec="none"))

    # Current Price (Teal)
    ax.text(label_x, last_close, f"{last_close:.2f}", transform=ax.get_yaxis_transform(),
            va="center", fontsize=7, color="white",
            bbox=dict(boxstyle="square,pad=0.2", fc="#26A69A", ec="none"))

    # Target (Yellow)
    ax.text(label_x, target_val, f"TGT: {target_val:.2f}", transform=ax.get_yaxis_transform(),
            va="center", fontsize=7, color="black",
            bbox=dict(boxstyle="square,pad=0.2", fc="yellow", ec="none"))

    # SL (Red)
    ax.text(label_x, sl_val, f"SL: {sl_val:.2f}", transform=ax.get_yaxis_transform(),
            va="center", fontsize=7, color="white",
            bbox=dict(boxstyle="square,pad=0.2", fc="#EF5350", ec="none"))

    ax.set_title(f"{symbol} – Setup", color=TEXT_COLOR, fontsize=10)
    fig.autofmt_xdate()
    
    path = os.path.join(get_temp_dir(), f"{symbol}.png")
    plt.savefig(path, dpi=140, bbox_inches="tight")
    plt.close()
    return path



# ================= MAIN (WITH DEBUG MOCK) =================
def main():
    symbols = pd.read_csv(SYMBOLS_CSV)
    signals_sent = 0
    last_processed_df = None
    last_processed_symbol = None

    for idx, row in symbols.iterrows():

        # if DEBUG== True and idx > len(symbols)//4 :
            # break

        if signals_sent >= MAX_SIGNALS:
            log("Max signal limit reached. Stopping scan.")
            break

        symbol = row.get("tradingsymbol") or row.get("symbol")
        key = row["instrument_key"]

        log(f"Fetching: {symbol}")

        df = fetch_data(key)
        if df is None:
            continue

        df = calculate_indicators(df)
        
        # Store for debug fallback
        last_processed_df = df
        last_processed_symbol = symbol
        
        signal = check_signal(df, symbol)

        if signal:
            log(f"SIGNAL FOUND: {symbol}")
            chart = generate_chart(df, symbol, signal)
            caption = (
                f"📈 {symbol}\n"
                f"Entry > {signal['Entry']}\n"
                f"SL: {signal['SL']} ({signal['SLP']}%)\n"
                f"Target: {signal['Target']}\n"
                f"RSI: {signal['RSI']}\n"
                f"Last Candle: {signal['LastDate']}"
            )
            send_telegram_photo(chart, caption)
            signals_sent += 1

        time.sleep(API_SLEEP)

    # 🔥 DEBUG MOCK SIGNAL: Trigger if no real signals found and DEBUG is True
    if DEBUG and signals_sent == 0 and last_processed_df is not None:
        log(f"DEBUG MODE: Generating Mock Signal for {last_processed_symbol}")
        curr_price = last_processed_df.iloc[-1]['close']
        
        # Create a fake signal dictionary for the chart
        mock_signal = {
            "Symbol": f"TEST-{last_processed_symbol}",
            "Entry": round(curr_price, 2),
            "SL": round(curr_price * 0.97, 2),      # 3% below
            "Target": round(curr_price * 1.06, 2),  # 6% above
            "RSI": 50.0,
            "SLP": 3.0,
            "LastDate": last_processed_df.iloc[-1]['timestamp'].date()
        }
        
        chart = generate_chart(last_processed_df, f"DEBUG_{last_processed_symbol}", mock_signal)
        send_telegram_photo(chart, f"🛠 DEBUG MOCK SIGNAL\nSymbol: {last_processed_symbol}\nThis is a test to check chart lines.")
        signals_sent += 1

    log(f"Scan finished | Signals sent: {signals_sent}")

    if signals_sent == 0:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": f"📉 Scan Complete ({datetime.date.today()}): No signals found."
            }
        )


if __name__ == "__main__":
    main()
