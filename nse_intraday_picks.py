import pandas as pd
import datetime
import math
import requests
from nsetools import Nse

# ─────────────────────────────────────────────────────────────────────────────
# 1. CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

CAPITAL_TOTAL       = 50000        # (Reference only; not used directly)
MARGIN_PER_TRADE    = 15000        # Margin allocated per stock
LEVERAGE            = 2            # 2× intraday leverage
MAX_SYMBOLS_PER_DAY = 3            # Pick up to 3 stocks per day
PREMKT_THRESHOLD    = 0.3          # % change vs prev close (pre-market + open move)
# (Since we dropped OI, set OI_THRESHOLD to 0, but we’ll not even check it below)
OI_THRESHOLD        = 0.0
SL_FACTOR           = 0.015        # Stop-loss = 1.5% below entry
TARGET_FACTOR       = 0.03         # Target₁ = 3% above entry
SECTOR_MIN_COUNT    = 2            # Require ≥2 stocks in the same sector to trade

# ─────────────────────────────────────────────────────────────────────────────
# 2. READ SYMBOL LIST
# ─────────────────────────────────────────────────────────────────────────────

def get_nifty_list():
    """
    Reads stock symbols from 'symbols.txt' and returns them as a list.
    """
    try:
        with open("symbols.txt", "r") as file:
            symbols = [line.strip().upper() for line in file if line.strip()]
        print(f"✔️ Loaded {len(symbols)} symbols from symbols.txt\n")
        return symbols
    except FileNotFoundError:
        print("⚠️ 'symbols.txt' not found. Please ensure the file exists in the script directory.")
        return []

# ─────────────────────────────────────────────────────────────────────────────
# 3. TELEGRAM BOT SETUP
# ─────────────────────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = None
TELEGRAM_CHAT_ID   = None

def send_telegram_message(text: str):
    """
    Sends `text` to the Telegram chat using BOT_TOKEN and CHAT_ID.
    Raises if either secret is not set.
    """
    global TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

    if TELEGRAM_BOT_TOKEN is None or TELEGRAM_CHAT_ID is None:
        raise RuntimeError("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set in environment")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    }
    resp = requests.post(url, data=payload)
    if not resp.ok:
        print("⚠️ Failed to send Telegram message:", resp.text)

# ─────────────────────────────────────────────────────────────────────────────
# 4. HELPERS FOR NSE DATA (via nsetools)
# ─────────────────────────────────────────────────────────────────────────────

nse = Nse()

def fetch_quote(symbol):
    """
    Fetches from nsetools:
      - last traded price (LTP)
      - previous close
      - industry/sector

    Returns a dict { 'symbol', 'cmp', 'pct_change', 'industry' }
    or None if any error occurs.
    """
    try:
        q = nse.get_quote(symbol)
        # 'lastPrice' and 'previousClose' come as strings or floats
        cmp_price  = float(q.get("lastPrice", 0.0))
        prev_close = float(q.get("previousClose", 0.0))
        if prev_close == 0.0:
            # no valid data
            print(f"❌ [{symbol}] invalid prev_close=0.0, skipping.")
            return None

        pct_change = ((cmp_price - prev_close) / prev_close) * 100
        industry   = q.get("industry", "Unknown") or "Unknown"

    except Exception as e:
        print(f"❌ [{symbol}] Failed to fetch quote via nsetools: {e}")
        return None

    # Debug print
    print(f"🔍 [{symbol}] cmp={cmp_price:.2f} | prev_close={prev_close:.2f} | pct_change={pct_change:.2f}% | industry='{industry}'")

    return {
        "symbol":     symbol,
        "cmp":        cmp_price,
        "pct_change": pct_change,
        "industry":   industry
    }

# ─────────────────────────────────────────────────────────────────────────────
# 5. MAIN SCRIPT
# ─────────────────────────────────────────────────────────────────────────────

def main():
    # 1) Load Telegram secrets from environment
    import os
    global TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")

    # 2) Step 1: Load our symbol list
    symbols = get_nifty_list()
    if not symbols:
        print("❌ No symbols to process. Exiting.")
        return

    # 3) Step 2: Fetch quote for each at ~9:30
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"⏱️ Starting fetch at {now} IST for {len(symbols)} symbols...\n")

    data = []
    for s in symbols:
        print(f"➡️ Fetching '{s}'…")
        info = fetch_quote(s)
        if not info:
            continue

        # Filter 1: % change vs prev close ≥ PREMKT_THRESHOLD
        if info["pct_change"] >= PREMKT_THRESHOLD:
            print(f"   ✅ [{s}] passed PREMKT_THRESHOLD ({info['pct_change']:.2f}% ≥ {PREMKT_THRESHOLD:.2f}%)")
            data.append(info)
        else:
            print(f"   ❎ [{s}] failed PREMKT_THRESHOLD ({info['pct_change']:.2f}% < {PREMKT_THRESHOLD:.2f}%)")

    if not data:
        msg = f"⏳ No stocks ≥ +{PREMKT_THRESHOLD:.1f}% at 09:30 on {datetime.date.today().isoformat()}."
        print("\n" + msg)
        send_telegram_message(msg)
        return

    df = pd.DataFrame(data)
    print(f"\n📊 {len(df)} symbols passed the 1st filter. Details:")
    print(df[["symbol", "pct_change", "industry"]].to_string(index=False))
    print()

    # 4) Step 3: “Momentum sector” filter (≥ SECTOR_MIN_COUNT winners in same industry)
    sector_counts = df["industry"].value_counts()
    print("📈 Sector counts among filtered symbols:")
    for sector, count in sector_counts.items():
        print(f"   • {sector}: {count} stock(s)")

    momentum_sectors = sector_counts[sector_counts >= SECTOR_MIN_COUNT].index.tolist()
    if not momentum_sectors:
        msg = (
            f"⚠️ No sector has ≥ {SECTOR_MIN_COUNT} stocks with +{PREMKT_THRESHOLD:.1f}% at 09:30.\n"
            f"Date: {datetime.date.today().isoformat()}"
        )
        print("\n" + msg)
        send_telegram_message(msg)
        return

    # Pick the sector with the highest count
    chosen_sector = sector_counts.idxmax()
    print(f"\n🎯 Chosen sector: {chosen_sector} ({sector_counts[chosen_sector]} winners)")

    df = df[df["industry"] == chosen_sector].copy()
    if df.empty:
        msg = f"⚠️ After sector filter, 0 stocks remain in {chosen_sector}."
        print("\n" + msg)
        send_telegram_message(msg)
        return

    print(f"   → {len(df)} stocks remain after selecting sector '{chosen_sector}': {df['symbol'].tolist()}\n")

    # 5) (No OI filter) → Move on to sorting & final picks
    df = df.sort_values(by="pct_change", ascending=False).head(MAX_SYMBOLS_PER_DAY)
    print(f"🏆 Top {MAX_SYMBOLS_PER_DAY} picks by %‐change:")
    print(df[["symbol", "pct_change"]].to_string(index=False), "\n")

    # 6) Build the Telegram message
    header = f"🟢 *9:30 Intraday Picks for {datetime.date.today().isoformat()}*\n"
    header += f"Sector in focus: *{chosen_sector}*\n\n"

    lines = []
    for _, row in df.iterrows():
        sym   = row["symbol"]
        cmp_p = row["cmp"]
        sl    = round(cmp_p * (1 - SL_FACTOR), 2)
        tgt   = round(cmp_p * (1 + TARGET_FACTOR), 2)
        qty   = math.floor((MARGIN_PER_TRADE * LEVERAGE) / cmp_p)

        trail_instr = (
            f"• At +1.5% → move SL → *{cmp_p:.2f}*\n"
            f"• At +2% → trail SL = (current_price × 0.99)"
        )

        lines.append(
            f"🔹 *{sym}*  \n"
            f"   Entry: *{cmp_p:.2f}*  \n"
            f"   SL: *{sl:.2f}*  |  Target₁: *{tgt:.2f}*  \n"
            f"   Qty (@2× Lev): *{qty}*  \n"
            f"{trail_instr}\n"
        )

    footer = (
        "\n⚠️ *Remember:*  \n"
        " • Place a *Bracket‐Order* if your broker supports it (Groww, AngelOne, Dhan).  \n"
        " • If no BO, place market/limit buy → set SL at above SL.  \n"
        " • Move SL to breakeven at +1.5%; trail SL by –1% once +2% hits.  \n"
        " • *Exit all positions by 10:30 AM* IST if neither SL nor target is hit.  \n"
        " • Stop trading for the day if you lose 2 full SLs (~₹1,500–₹2,000)."
    )

    full_message = header + "\n".join(lines) + footer

    print("✉️ Sending Telegram message with final picks…\n")
    send_telegram_message(full_message)


if __name__ == "__main__":
    main()
