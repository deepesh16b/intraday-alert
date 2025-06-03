import pandas as pd
import datetime
import math
import requests
from nsepython import nse_eq, nse_fno

# ─────────────────────────────────────────────────────────────────────────────
# 1. CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

CAPITAL_TOTAL       = 50000        # (Reference only; not used directly)
MARGIN_PER_TRADE    = 15000        # Margin allocated per stock
LEVERAGE            = 2            # 2× intraday leverage
MAX_SYMBOLS_PER_DAY = 3            # Pick up to 3 stocks per day
PREMKT_THRESHOLD    = 0.3          # % change vs prev close (pre-market + open move)
OI_THRESHOLD        = 2.0          # % increase in Futures OI to qualify
SL_FACTOR           = 0.015        # Stop-loss = 1.5% below entry
TARGET_FACTOR       = 0.03         # Target₁ = 3% above entry
SECTOR_MIN_COUNT    = 2          # Require ≥3 stocks in the same sector to trade

# ─────────────────────────────────────────────────────────────────────────────
# 2. HARD-CODED NIFTY 200 LIST
# ─────────────────────────────────────────────────────────────────────────────


def get_nifty_list():
    """
    Reads stock symbols from 'symbols.txt' and returns them as a list.
    """
    try:
        with open("symbols.txt", "r") as file:
            symbols = [line.strip().upper() for line in file if line.strip()]
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
# 4. HELPERS FOR NSE DATA
# ─────────────────────────────────────────────────────────────────────────────



def fetch_quote_and_oi(symbol):
    """
    Fetches from nsepython:
      - LTP (last traded price)
      - Previous close
      - Industry/Sector
      - Futures Open Interest (today vs yesterday) % change

    Returns a dict { 'symbol', 'cmp', 'pct_change', 'industry', 'oi_pct_change' }
    or None if any error occurs.
    """
    # 1) Equity quote via nse_eq(...)
    try:
        q = nse_eq(symbol)
        cmp_price  = float(q["priceInfo"]["lastPrice"])
        prev_close = float(q["priceInfo"]["close"])
        pct_change = ((cmp_price - prev_close) / prev_close) * 100
        industry   = q["metadata"].get("pdSectorInd", "Unknown")
    except Exception:
        return None

    # 2) F&O‐chain via nse_fno(...)
    try:
        fno = nse_fno(symbol)
        # Sum up "openInterest" for today vs yesterday
        oi_today    = sum(item.get("openInterest", 0) for item in fno["data"])
        oi_prev_day = sum((item.get("openInterest", 0) - item.get("changeInOI", 0)) for item in fno["data"])
        oi_pct_change = ((oi_today - oi_prev_day) / oi_prev_day) * 100 if oi_prev_day > 0 else 0.0
    except Exception:
        oi_pct_change = 0.0

    return {
        "symbol":        symbol,
        "cmp":           cmp_price,
        "pct_change":    pct_change,
        "industry":      industry,
        "oi_pct_change": oi_pct_change
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

    # 2) Step 1: Use our NIFTY 200 list
    symbols = get_nifty_list()

    # 3) Step 2: Fetch quote + OI for each at 9:30
    data = []
    for s in symbols:
        info = fetch_quote_and_oi(s)
        if not info:
            continue
        # Filter 1: % change vs prev close ≥ PREMKT_THRESHOLD
        if info["pct_change"] >= PREMKT_THRESHOLD:
            data.append(info)

    if not data:
        msg = f"⏳ No NIFTY 200 stocks ≥ +{PREMKT_THRESHOLD:.1f}% at 09:30 on {datetime.date.today().isoformat()}."
        send_telegram_message(msg)
        return

    df = pd.DataFrame(data)

    # 4) Step 3: “Momentum sector” filter (≥ SECTOR_MIN_COUNT winners in the same industry)
    sector_counts = df["industry"].value_counts()
    momentum_sectors = sector_counts[sector_counts >= SECTOR_MIN_COUNT].index.tolist()
    if not momentum_sectors:
        msg = (
            f"⚠️ No sector has ≥ {SECTOR_MIN_COUNT} stocks with +{PREMKT_THRESHOLD:.1f}% at 09:30.\n"
            f"Date: {datetime.date.today().isoformat()}"
        )
        send_telegram_message(msg)
        return

    # Pick the single sector with the highest count
    chosen_sector = sector_counts.idxmax()
    df = df[df["industry"] == chosen_sector].copy()
    if df.empty:
        msg = f"⚠️ After sector filter, 0 stocks remain in {chosen_sector}."
        send_telegram_message(msg)
        return

    # 5) Step 4: OI filter ≥ OI_THRESHOLD
    df = df[df["oi_pct_change"] >= OI_THRESHOLD].copy()
    if df.empty:
        msg = (
            f"⚠️ No stocks in sector '{chosen_sector}' have OI Δ ≥ {OI_THRESHOLD:.1f}% at 09:30.\n"
            f"Date: {datetime.date.today().isoformat()}"
        )
        send_telegram_message(msg)
        return

    # 6) Sort by pct_change (descending) and pick top MAX_SYMBOLS_PER_DAY
    df = df.sort_values(by="pct_change", ascending=False).head(MAX_SYMBOLS_PER_DAY)

    # 7) Build the Markdown Telegram message
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
            f"   OI Δ: *{row['oi_pct_change']:.2f}%*  \n"
            f"   Qty (@2× Lev): *{qty}*  \n"
            f"{trail_instr}\n"
        )

    footer = (
        "\n⚠️ *Remember:*  ￼\n"
        " • Place a *Bracket‐Order* if your broker supports it (Groww, AngelOne, Dhan).  ￼\n"
        " • If no BO, place market/limit buy → set SL at above SL.  ￼\n"
        " • Move SL to breakeven at +1.5%; trail SL by –1% once +2% hits.  ￼\n"
        " • *Exit all positions by 10:30 AM* IST if neither SL nor target is hit.  ￼\n"
        " • Stop trading for the day if you lose 2 full SLs (~₹1,500–₹2,000)."
    )

    full_message = header + "\n".join(lines) + footer
    send_telegram_message(full_message)


if __name__ == "__main__":
    main()
