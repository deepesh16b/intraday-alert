import os
import math
import datetime
import time
import pandas as pd
import requests
import yfinance as yf

# ─────────────────────────────────────────────────────────────────────────────
# 1. CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

CAPITAL_TOTAL        = 50000         # (Reference only; not used directly)
MARGIN_PER_TRADE     = 15000         # Margin allocated per stock
LEVERAGE             = 2             # 2× intraday leverage
MAX_SYMBOLS_PER_DAY  = 3             # Pick up to 3 stocks per day

PREMKT_THRESHOLD     = 2.0           # % change vs prev close at ~9:30
VOL_SPIKE_THRESHOLD  = 200.0         # % spike: today’s 5-min vol ≥ (1 + 2.0)×avg_5min_volume (3×)
RSI_LOWER_THRESHOLD  = 50.0          # 14-day RSI must be ≥ 50
RSI_UPPER_THRESHOLD  = 80.0          # 14-day RSI must be ≤ 80 (avoid extreme overbought)

SL_FACTOR            = 0.015         # Stop‐loss = 1.5% below entry
TARGET_FACTOR        = 0.03          # Target₁ = 3% above entry
SECTOR_MIN_COUNT     = 2             # Require ≥2 stocks in same sector to trade

SYMBOLS_FILE         = "symbols.txt" # One ticker per line, e.g. RELIANCE, TCS, etc.
RSI_PERIOD           = 14            # 14-day RSI

# ─────────────────────────────────────────────────────────────────────────────
# 2. TELEGRAM BOT SETUP
# ─────────────────────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = None
TELEGRAM_CHAT_ID   = None

def send_telegram_message(text: str):
    """
    Sends plain‐text `text` to the Telegram chat using BOT_TOKEN and CHAT_ID.
    """
    global TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    if TELEGRAM_BOT_TOKEN is None or TELEGRAM_CHAT_ID is None:
        raise RuntimeError("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set in environment")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text
    }
    resp = requests.post(url, data=payload)
    if not resp.ok:
        print("⚠️ Failed to send Telegram message:", resp.text)

# ─────────────────────────────────────────────────────────────────────────────
# 3. READ & DEDUPE SYMBOL LIST
# ─────────────────────────────────────────────────────────────────────────────

def get_nifty_list():
    """
    Reads stock symbols from SYMBOLS_FILE, uppercases and dedupes them.
    """
    try:
        with open(SYMBOLS_FILE, "r") as file:
            raw = [line.strip().upper() for line in file if line.strip()]
        # Dedupe while preserving order:
        symbols = list(dict.fromkeys(raw))
        print(f"✔️ Loaded {len(raw)} raw lines, {len(symbols)} unique symbols from {SYMBOLS_FILE}\n")
        return symbols
    except FileNotFoundError:
        print(f"⚠️ '{SYMBOLS_FILE}' not found. Please ensure the file exists.")
        return []

# ─────────────────────────────────────────────────────────────────────────────
# 4. RSI CALCULATION (14-day, daily) VIA PANDAS
# ─────────────────────────────────────────────────────────────────────────────

def compute_14day_rsi(symbol):
    """
    Fetch last ~30 trading days of daily closes and compute the 14-day RSI.
    Returns a float RSI value or None if fetch fails.
    """
    yf_symbol = symbol + ".NS"
    try:
        t = yf.Ticker(yf_symbol)
        hist = t.history(period="1mo", interval="1d", actions=False)
        closes = hist["Close"].dropna()
        if len(closes) < RSI_PERIOD + 1:
            return None

        delta = closes.diff().dropna()
        gain = delta.where(delta > 0, 0.0)
        loss = -delta.where(delta < 0, 0.0)

        avg_gain = gain.rolling(window=RSI_PERIOD).mean()
        avg_loss = loss.rolling(window=RSI_PERIOD).mean()
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        # Return the most recent RSI
        return float(rsi.iloc[-1])
    except Exception:
        return None

# ─────────────────────────────────────────────────────────────────────────────
# 5. FETCH SPOT + VOLUME‐SPIKE + RSI
# ─────────────────────────────────────────────────────────────────────────────

def fetch_quote_vol_rsi(symbol):
    """
    1) Fetch spot data from yfinance: LTP, previous close, average daily volume, sector.
    2) Compute 14-day RSI and require RSI_LOWER_THRESHOLD ≤ RSI ≤ RSI_UPPER_THRESHOLD.
    3) Fetch today's 1m bars (period='1d', interval='1m'), convert to IST, sum volume 09:30–09:35.
    4) Compute pct_change and vol_spike_pct.
    Returns a dict:
      {
        'symbol':        symbol,
        'cmp':           LTP,
        'pct_change':    pct_change,
        'sector':        sector,
        'vol_spike_pct': vol_spike_pct (or None if intraday fails),
        'rsi':           RSI value (or None if failed)
      }
    or None if spot or RSI fetch fails.
    """
    yf_symbol = symbol + ".NS"
    try:
        t = yf.Ticker(yf_symbol)
        info = t.info
        cmp_price     = float(info.get("regularMarketPrice", 0.0))
        prev_close    = float(info.get("previousClose", 0.0))
        avg_daily_vol = float(info.get("averageVolume", 0.0))
        sector        = info.get("sector", "Unknown") or "Unknown"
    except Exception as e:
        print(f"❌ [{symbol}] yfinance error (spot): {e}")
        return None

    if prev_close == 0.0 or avg_daily_vol == 0.0:
        print(f"❌ [{symbol}] invalid prev_close or avg_daily_vol = 0, skipping.")
        return None

    pct_change = ((cmp_price - prev_close) / prev_close) * 100

    # 14-day RSI
    rsi = compute_14day_rsi(symbol)
    if rsi is None:
        print(f"⚠️ [{symbol}] RSI computation failed or insufficient data.")
        return None
    if not (RSI_LOWER_THRESHOLD <= rsi <= RSI_UPPER_THRESHOLD):
        print(f"   ❎ [{symbol}] RSI {rsi:.2f} not in [{RSI_LOWER_THRESHOLD}-{RSI_UPPER_THRESHOLD}], skipping.")
        return None

    # Average 5-min volume
    avg_5min_vol = avg_daily_vol / 78.0  # ~78 five-minute bars per 6½-hour day

    # Intraday 1m data for today
    vol_spike_pct = None
    try:
        hist = t.history(period="1d", interval="1m", actions=False)
        if hist.empty or "Volume" not in hist.columns:
            print(f"⚠️ [{symbol}] No intraday data returned by yfinance.")
            vol_spike_pct = None
        else:
            hist = hist.tz_convert("Asia/Kolkata")
            start_time = datetime.time(9, 23)
            end_time   = datetime.time(9, 28)
            mask = (hist.index.time >= start_time) & (hist.index.time < end_time)
            first_5min = hist.loc[mask]

            if not first_5min.empty:
                today_5min_vol = int(first_5min["Volume"].sum())
                vol_spike_pct = ((today_5min_vol - avg_5min_vol) / avg_5min_vol) * 100
            else:
                print(f"⚠️ [{symbol}] No bars between 09:23–09:28 IST.")
                vol_spike_pct = None
    except Exception as e:
        print(f"❌ [{symbol}] Intraday fetch failed: {e}")
        vol_spike_pct = None

    # Debug print
    vs_text = f"{vol_spike_pct:.2f}%" if vol_spike_pct is not None else "N/A"
    print(
        f"🔍 [{symbol}] spot_pct={pct_change:.2f}% | RSI={rsi:.2f} | sector='{sector}' | 5m Vol-Spike={vs_text}"
    )

    return {
        "symbol":        symbol,
        "cmp":           cmp_price,
        "pct_change":    pct_change,
        "sector":        sector,
        "vol_spike_pct": vol_spike_pct,
        "rsi":           rsi
    }

# ─────────────────────────────────────────────────────────────────────────────
# 6. MAIN SCRIPT
# ─────────────────────────────────────────────────────────────────────────────

def main():
    # 1) Load Telegram secrets from environment
    global TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")

    # 2) Load & dedupe symbol list
    symbols = get_nifty_list()
    if not symbols:
        print("❌ No symbols to process. Exiting.")
        return

    # 3) Fetch data at ~09:30 IST
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"⏱️ Starting fetch at {now} IST for {len(symbols)} symbols...\n")

    rows = []
    for s in symbols:
        print(f"➡️ Fetching '{s}'…")
        info = fetch_quote_vol_rsi(s)
        if not info:
            continue

        # Filter A: spot % change ≥ PREMKT_THRESHOLD
        if info["pct_change"] < PREMKT_THRESHOLD:
            print(f"   ❎ [{s}] spot_pct {info['pct_change']:.2f}% < {PREMKT_THRESHOLD:.2f}%")
            continue

        # Filter B: 5-min volume spike ≥ VOL_SPIKE_THRESHOLD
        if info["vol_spike_pct"] is None or info["vol_spike_pct"] < VOL_SPIKE_THRESHOLD:
            vs = info["vol_spike_pct"]
            vs_txt = f"{vs:.2f}%" if vs is not None else "N/A"
            print(f"   ❎ [{s}] 5m Vol-Spike {vs_txt} < {VOL_SPIKE_THRESHOLD:.2f}%")
            continue

        # Passed all filters
        rows.append(info)

        # Short sleep to avoid yfinance rate‐limit issues
        time.sleep(0.5)

    if not rows:
        msg = (
            f"⏳ No stocks ≥ +{PREMKT_THRESHOLD:.1f}% , RSI in [{RSI_LOWER_THRESHOLD}-{RSI_UPPER_THRESHOLD}], "
            f"and 5m Vol-Spike ≥ {VOL_SPIKE_THRESHOLD:.1f}% at 09:30 on "
            f"{datetime.date.today().isoformat()}."
        )
        print("\n" + msg)
        send_telegram_message(msg)
        return

    df = pd.DataFrame(rows)
    print(f"\n📊 {len(df)} symbols passed all filters. Details:")
    print(df[["symbol", "pct_change", "rsi", "sector", "vol_spike_pct"]].to_string(index=False))
    print()

    # 4) Momentum‐sector filter
    sector_counts = df["sector"].value_counts()
    print("📈 Sector counts among filtered symbols:")
    for sector, count in sector_counts.items():
        print(f"   • {sector}: {count} stock(s)")

    momentum_sectors = sector_counts[sector_counts >= SECTOR_MIN_COUNT].index.tolist()
    if not momentum_sectors:
        msg = (
            f"⚠️ No sector has ≥ {SECTOR_MIN_COUNT} stocks meeting all filters at 09:30.\n"
            f"Date: {datetime.date.today().isoformat()}"
        )
        print("\n" + msg)
        send_telegram_message(msg)
        return

    chosen_sector = sector_counts.idxmax()
    print(f"\n🎯 Chosen sector: {chosen_sector} ({sector_counts[chosen_sector]} winners)")

    df_sector = df[df["sector"] == chosen_sector].copy()
    if df_sector.empty:
        msg = f"⚠️ After sector filter, 0 stocks remain in {chosen_sector}."
        print("\n" + msg)
        send_telegram_message(msg)
        return

    print(f"   → {len(df_sector)} stocks remain in '{chosen_sector}': {df_sector['symbol'].tolist()}\n")

    # 5) Sort by spot % change and pick top MAX_SYMBOLS_PER_DAY
    df_sector = df_sector.sort_values(by="pct_change", ascending=False).head(MAX_SYMBOLS_PER_DAY)
    print(f"🏆 Top {MAX_SYMBOLS_PER_DAY} picks by %‐change:")
    print(df_sector[["symbol", "pct_change", "rsi", "vol_spike_pct"]].to_string(index=False), "\n")

    # 6) Build Telegram message (plain text)
    header = (
        "🟢 9:30 Intraday Picks for {}\n"
        "Sector in focus: {}\n\n"
    ).format(datetime.date.today().isoformat(), chosen_sector)

    lines = []
    for _, row in df_sector.iterrows():
        sym         = row["symbol"]
        cmp_p       = row["cmp"]
        sl          = round(cmp_p * (1 - SL_FACTOR), 2)
        tgt         = round(cmp_p * (1 + TARGET_FACTOR), 2)
        qty         = math.floor((MARGIN_PER_TRADE * LEVERAGE) / cmp_p)
        vs_pct      = row["vol_spike_pct"]
        vs_display  = f"{vs_pct:.2f}%"
        rsi_val     = row["rsi"]

        block = (
            f"🔹 {sym}\n"
            f"   Entry: {cmp_p:.2f}\n"
            f"   SL: {sl:.2f}  |  Target₁: {tgt:.2f}\n"
            f"   RSI: {rsi_val:.2f}  |  5m Vol-Spike: {vs_display}\n"
            f"   Qty (@2× Lev): {qty}\n"
            f"   • At +1.5% → move SL → {cmp_p:.2f}\n"
            f"   • At +2% → trail SL = (current_price × 0.99)\n"
        )
        lines.append(block)

    footer = (
        "\n⚠️ Remember:\n"
        " • Place a Bracket-Order if your broker supports it (Groww, AngelOne, Dhan).\n"
        " • If no BO, place market/limit buy → set SL at the SL level.\n"
        " • Move SL to breakeven at +1.5%; trail SL by –1% once +2% hits.\n"
        " • Exit all positions by 10:30 AM IST if neither SL nor target is hit.\n"
        " • Stop trading for the day if you lose 2 full SLs (~₹1,500–₹2,000).\n"
    )

    full_message = header + "\n".join(lines) + footer

    print("✉️ Sending Telegram message with final picks…\n")
    send_telegram_message(full_message)

if __name__ == "__main__":
    main()
