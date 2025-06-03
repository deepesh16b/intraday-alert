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

CAPITAL_TOTAL        = 50000        # (Reference only; not used directly)
MARGIN_PER_TRADE     = 15000        # Margin allocated per stock
LEVERAGE             = 2            # 2× intraday leverage
MAX_SYMBOLS_PER_DAY  = 3            # Pick up to 3 stocks per day
PREMKT_THRESHOLD     = 0.3          # % change vs prev close (pre‐market + open move)
OI_THRESHOLD         = 7.0          # % increase in Futures OI to qualify
SL_FACTOR            = 0.015        # Stop‐loss = 1.5% below entry
TARGET_FACTOR        = 0.03         # Target₁ = 3% above entry
SECTOR_MIN_COUNT     = 2            # Require ≥2 stocks in the same sector to trade
SYMBOLS_FILE         = "symbols.txt"  # One ticker per line, e.g. RELIANCE, TCS, etc.

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
# 3. READ SYMBOL LIST
# ─────────────────────────────────────────────────────────────────────────────

def get_nifty_list():
    """
    Reads stock symbols from SYMBOLS_FILE and returns them as a list.
    """
    try:
        with open(SYMBOLS_FILE, "r") as file:
            symbols = [line.strip().upper() for line in file if line.strip()]
        print(f"✔️ Loaded {len(symbols)} symbols from {SYMBOLS_FILE}\n")
        return symbols
    except FileNotFoundError:
        print(f"⚠️ '{SYMBOLS_FILE}' not found. Please ensure the file exists in the script directory.")
        return []

# ─────────────────────────────────────────────────────────────────────────────
# 4. FETCH QUOTE + OI (with NSE JSON scraping)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_quote_and_oi(symbol):
    """
    1) Fetch spot data from yfinance: LTP, previous close, sector.
    2) Attempt to fetch futures‐chain JSON from NSE to compute total OI % change.
       If that fails, set 'oi_pct_change' = None.
    Returns a dict { 'symbol', 'cmp', 'pct_change', 'sector', 'oi_pct_change' }
    or None if spot quote fails.
    """
    # --- A) Fetch spot via yfinance ---
    yf_symbol = symbol + ".NS"
    try:
        t = yf.Ticker(yf_symbol)
        info = t.info
        cmp_price   = float(info.get("regularMarketPrice", 0.0))
        prev_close  = float(info.get("previousClose", 0.0))
        sector      = info.get("sector", "Unknown") or "Unknown"
    except Exception as e:
        print(f"❌ [{symbol}] yfinance error: {e}")
        return None

    if prev_close == 0.0:
        print(f"❌ [{symbol}] previousClose=0.0, skipping.")
        return None

    pct_change = ((cmp_price - prev_close) / prev_close) * 100

    # --- B) Fetch futures-chain JSON from NSE (scraping) ---
    oi_pct_change = None  # default if fetch fails
    base_url = f"https://www.nseindia.com/api/future-chain-equity?symbol={symbol}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/114.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/option-chain",
        "Origin": "https://www.nseindia.com"
    }

    session = requests.Session()
    session.headers.update(headers)

    # Step 1: hit homepage to get cookies
    try:
        session.get("https://www.nseindia.com", timeout=5)
    except Exception as e:
        print(f"⚠️ [{symbol}] NSE homepage request failed: {e}")

    # Step 2: request futures‐chain JSON
    try:
        resp = session.get(base_url, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        records = data.get("records", {}).get("data", [])
        if isinstance(records, list) and len(records) > 0:
            oi_today = sum(item.get("openInterest", 0) for item in records)
            oi_prev_day = sum(item.get("openInterestPrevDay", 0) for item in records)
            if oi_prev_day > 0:
                oi_pct_change = ((oi_today - oi_prev_day) / oi_prev_day) * 100
            else:
                oi_pct_change = 0.0
        else:
            print(f"⚠️ [{symbol}] No records in futures‐chain JSON.")
            oi_pct_change = None
    except Exception as e:
        print(f"❌ [{symbol}] Failed to fetch/parsing futures‐chain JSON: {e}")
        oi_pct_change = None

    # Debug print
    oi_text = f"{oi_pct_change:.2f}%" if oi_pct_change is not None else "N/A"
    print(
        f"🔍 [{symbol}] spot_pct={pct_change:.2f}% | sector='{sector}' | OI Δ = {oi_text}"
    )

    return {
        "symbol":        symbol,
        "cmp":           cmp_price,
        "pct_change":    pct_change,
        "sector":        sector,
        "oi_pct_change": oi_pct_change
    }

# ─────────────────────────────────────────────────────────────────────────────
# 5. MAIN SCRIPT
# ─────────────────────────────────────────────────────────────────────────────

def main():
    # 1) Load Telegram secrets from environment
    global TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")

    # 2) Load symbol list
    symbols = get_nifty_list()
    if not symbols:
        print("❌ No symbols to process. Exiting.")
        return

    # 3) Fetch data at ~09:30 IST
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"⏱️ Starting fetch at {now} IST for {len(symbols)} symbols...\n")

    data_rows = []
    for s in symbols:
        print(f"➡️ Fetching '{s}'…")
        info = fetch_quote_and_oi(s)
        if not info:
            continue

        # Filter A: spot % change ≥ PREMKT_THRESHOLD
        if info["pct_change"] < PREMKT_THRESHOLD:
            print(f"   ❎ [{s}] spot_pct {info['pct_change']:.2f}% < {PREMKT_THRESHOLD:.2f}%")
            continue

        # Keep the row regardless of OI fetch; OI filter will apply later
        data_rows.append(info)

        # To reduce chances of 429/403, add a short sleep
        time.sleep(0.5)

    if not data_rows:
        msg = f"⏳ No stocks ≥ +{PREMKT_THRESHOLD:.1f}% at 09:30 on {datetime.date.today().isoformat()}."
        print("\n" + msg)
        send_telegram_message(msg)
        return

    df = pd.DataFrame(data_rows)
    print(f"\n📊 {len(df)} symbols passed spot filter. Details:")
    print(df[["symbol", "pct_change", "sector", "oi_pct_change"]].to_string(index=False))
    print()

    # 4) Momentum‐sector filter (among those passing spot filter)
    sector_counts = df["sector"].value_counts()
    print("📈 Sector counts among filtered symbols:")
    for sector, count in sector_counts.items():
        print(f"   • {sector}: {count} stock(s)")

    # Keep sectors with at least SECTOR_MIN_COUNT
    momentum_sectors = sector_counts[sector_counts >= SECTOR_MIN_COUNT].index.tolist()
    if not momentum_sectors:
        msg = (
            f"⚠️ No sector has ≥ {SECTOR_MIN_COUNT} stocks with +{PREMKT_THRESHOLD:.1f}% at 09:30.\n"
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

    # 5) Within that sector, apply OI filter if available
    # Split into those with valid OI and those where OI fetch failed
    df_valid_oi = df_sector[df_sector["oi_pct_change"].notnull()].copy()
    df_no_oi    = df_sector[df_sector["oi_pct_change"].isnull()].copy()

    # Among valid OI, keep those ≥ OI_THRESHOLD
    df_valid_oi = df_valid_oi[df_valid_oi["oi_pct_change"] >= OI_THRESHOLD].copy()

    picks = []
    note_oi_failed = False

    if not df_valid_oi.empty:
        # Sort by spot % change and pick top
        df_valid_oi = df_valid_oi.sort_values(by="pct_change", ascending=False)
        picks = df_valid_oi.head(MAX_SYMBOLS_PER_DAY).to_dict("records")

        # If fewer than MAX_SYMBOLS_PER_DAY, fill from df_no_oi (order by spot %)
        if len(picks) < MAX_SYMBOLS_PER_DAY and not df_no_oi.empty:
            df_no_oi = df_no_oi.sort_values(by="pct_change", ascending=False)
            needed = MAX_SYMBOLS_PER_DAY - len(picks)
            for _, row in df_no_oi.head(needed).iterrows():
                picks.append(row.to_dict())
                note_oi_failed = True
    else:
        # No valid OI‐filtered picks; use those where OI fetch failed (if any)
        if not df_no_oi.empty:
            df_no_oi = df_no_oi.sort_values(by="pct_change", ascending=False)
            picks = df_no_oi.head(MAX_SYMBOLS_PER_DAY).to_dict("records")
            note_oi_failed = True
        else:
            # No picks at all
            msg = (
                f"⚠️ No stocks in sector '{chosen_sector}' have OI Δ ≥ {OI_THRESHOLD:.1f}% "
                f"and OI fetch succeeded at 09:30.\nDate: {datetime.date.today().isoformat()}"
            )
            print("\n" + msg)
            send_telegram_message(msg)
            return

    # 6) Build Telegram message
    header = (
        "🟢 9:30 Intraday Picks for {}\n"
        "Sector in focus: {}\n\n"
    ).format(datetime.date.today().isoformat(), chosen_sector)

    lines = []
    for info in picks:
        sym        = info["symbol"]
        cmp_p      = info["cmp"]
        sl         = round(cmp_p * (1 - SL_FACTOR), 2)
        tgt        = round(cmp_p * (1 + TARGET_FACTOR), 2)
        qty        = math.floor((MARGIN_PER_TRADE * LEVERAGE) / cmp_p)
        oi_pct     = info["oi_pct_change"]
        oi_display = f"{oi_pct:.2f}%" if oi_pct is not None else "N/A"

        block = (
            f"🔹 {sym}\n"
            f"   Entry: {cmp_p:.2f}\n"
            f"   SL: {sl:.2f}  |  Target₁: {tgt:.2f}\n"
            f"   OI Δ: {oi_display}\n"
            f"   Qty (@2× Lev): {qty}\n"
            f"   • At +1.5% → move SL → {cmp_p:.2f}\n"
            f"   • At +2% → trail SL = (current_price × 0.99)\n"
        )
        lines.append(block)

    footer = (
        "\n⚠️ Remember:\n"
        " • Place a Bracket‐Order if your broker supports it (Groww, AngelOne, Dhan).\n"
        " • If no BO, place market/limit buy → set SL at the SL level.\n"
        " • Move SL to breakeven at +1.5%; trail SL by –1% once +2% hits.\n"
        " • Exit all positions by 10:30 AM IST if neither SL nor target is hit.\n"
        " • Stop trading for the day if you lose 2 full SLs (~₹1,500–₹2,000).\n"
    )
    if note_oi_failed:
        footer += "⚠️ OI fetch failed for some picks; OI filter was not applied to them.\n"

    full_message = header + "\n".join(lines) + footer

    print("✉️ Sending Telegram message with final picks…\n")
    send_telegram_message(full_message)

if __name__ == "__main__":
    main()
