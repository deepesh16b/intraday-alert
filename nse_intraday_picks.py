# File: nse_intraday_picks.py

import pandas as pd
import datetime
import math
import requests
from nsepython import get_quote, fut_chain_oi

# ─────────────────────────────────────────────────────────────────────────────
# 1. CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

CAPITAL_TOTAL       = 50000        # (Reference only; not used directly)
MARGIN_PER_TRADE    = 15000        # Margin allocated per stock
LEVERAGE            = 2            # 2× intraday leverage
MAX_SYMBOLS_PER_DAY = 3            # Pick up to 3 stocks per day
PREMKT_THRESHOLD    = 2.0          # % change vs prev close (pre-market + open move)
OI_THRESHOLD        = 7.0          # % increase in Futures OI to qualify
SL_FACTOR           = 0.015        # Stop-loss = 1.5% below entry
TARGET_FACTOR       = 0.03         # Target₁ = 3% above entry
SECTOR_MIN_COUNT    = 3            # Require ≥3 stocks in the same sector to trade

# ─────────────────────────────────────────────────────────────────────────────
# 2. HARD-CODED NIFTY 200 LIST
# ─────────────────────────────────────────────────────────────────────────────

# This is the full list of NIFTY 200 symbols (as traded on NSE).
# The script will loop over all these tickers at 09:30 AM IST.
NIFTY200_SYMBOLS = [
    "3MINDIA","AARTIDRUGS","AARTIIND","ABB","ABCAPITAL","ABFRL","ACC","ADANIENT","ADANIPORTS",
    "ADANITRANS","AIAENG","AJANTPHARM","AKZOINDIA","ALEMBICLTD","ALKEM","ALKYLAMINE","ALLCARGO",
    "AMARAJABAT","AMBUJACEM","APLLTD","APOLLOHOSP","APOLLOTYRE","ASHOKLEY","ASIANPAINT","ASTRAL",
    "ATUL","AUBANK","AUROPHARMA","AXISBANK","BAJAJ-AUTO","BAJAJCON","BAJAJELEC","BAJFINANCE",
    "BAJAJFINSV","BAJAJHLDNG","BALKRISIND","BANDHANBNK","BANKBARODA","BANKINDIA","BATAINDIA","BBTC",
    "BEL","BERGEPAINT","BHARATFORG","BHARTIARTL","BHEL","BIOCON","BJP","BLUEDART","BOSCHLTD","BPCL",
    "BRITANNIA","CADILAHC","CANBK","CASTROLIND","CEATLTD","CENTURYTEX","CESC","CGPOWER","CHAMBLFERT",
    "CHOLAFIN","CIPLA","COALINDIA","COFORGE","COLPAL","CONCOR","COROMANDEL","CROMPTON","CUB","CYIENT",
    "DABUR","DALMIABHAI","DCBBANK","DEEPAKFERT","DEEPAKNTR","DIVISLAB","DIXON","DLF","DRREDDY","EICHERMOT",
    "EIDPARRY","EMAMILTD","ENDURANCE","ESCORTS","EXIDEIND","FDC","FEDERALBNK","FINCABLES","FINOLEXIND",
    "FSL","GAIL","GAYAPROJ","GMRINFRA","GNFC","GODREJCP","GODREJPROP","GRANULES","GRAPHITE","GRASIM",
    "GRUH","GUJGASLTD","HAL","HAVELLS","HCLTECH","HDFC","HDFCBANK","HDFCLIFE","HEROMOTOCO","HEXAWARE",
    "HINDALCO","HINDCOPPER","HINDPETRO","HINDUNILVR","HINDZINC","HONAUT","IBULHSGFIN","ICICIBANK","ICICIGI",
    "ICICIPRULI","IDEA","IDFCFIRSTB","IEX","IGL","INDIANB","INDIGO","INDUSINDBK","INDUSTOWER","INFIBEAM",
    "INFRATEL","INGERRAND","INTELLECT","IOC","IRCTC","ITC","JBCHEPHARM","JINDALSTEL","JSWENERGY","JSWSTEEL",
    "JUBLFOOD","JUSTDIAL","KOTAKBANK","KPITTECH","KRBL","KSCL","L&TFH","LICHSGFIN","LT","LTI","LTTS","LUPIN",
    "M&M","M&MFIN","MAHABANK","MAHINDCIE","MINDTREE","MRF","MUTHOOTFIN","NAM-INDIA","NATIONALUM","NBCC",
    "NCC","NHPC","NMDC","NTPC","ONGC","PAGEIND","PEL","PERSISTENT","PETRONET","PIDILITIND","PIIND","PNB",
    "POLYCAB","PRAJIND","PRESTIGE","PVR","RAMCOCEM","RBLBANK","RECLTD","RELIANCE","SRF","SAIL","SBILIFE",
    "SBIN","SCHAEFFLER","SHREECEM","SIEMENS","SRTRANSFIN","SUNPHARMA","SUNTV","SWSOLAR","SYNDIBANK","TATACHEM",
    "TATACONSUM","TATAMOTORS","TATAMTRDVR","TATAPOWER","TATASTEEL","TCS","TECHM","TITAN","TORNTPOWER","TRENT",
    "TVSMOTOR","UBL","UJJIVAN","ULTRACEMCO","UPL","VOLTAS","WIPRO","YESBANK","ZEEL"
]

# ─────────────────────────────────────────────────────────────────────────────
# 3. TELEGRAM BOT SETUP
# ─────────────────────────────────────────────────────────────────────────────

# These environment variables will be injected by GitHub Actions
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

def get_nifty_list():
    """
    Returns our hard-coded list of 200 NIFTY tickers.
    """
    return NIFTY200_SYMBOLS

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
    try:
        q = get_quote(symbol)
        cmp_price    = float(q['lastPrice'])
        prev_close   = float(q['previousClose'])
        pct_change   = ((cmp_price - prev_close) / prev_close) * 100
        industry     = q.get('industry', 'Unknown')
    except Exception:
        return None

    try:
        chain_df, _ = fut_chain_oi(symbol)
        oi_today       = chain_df['OI'].sum()
        oi_prev_day    = chain_df['OI (Previous Day)'].sum()
        oi_pct_change  = ((oi_today - oi_prev_day) / oi_prev_day) * 100 if oi_prev_day > 0 else 0.0
    except Exception:
        oi_pct_change = 0.0

    return {
        'symbol':        symbol,
        'cmp':           cmp_price,
        'pct_change':    pct_change,
        'industry':      industry,
        'oi_pct_change': oi_pct_change
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
        if info['pct_change'] >= PREMKT_THRESHOLD:
            data.append(info)

    if not data:
        msg = f"⏳ No NIFTY 200 stocks ≥ +{PREMKT_THRESHOLD:.1f}% at 09:30 on {datetime.date.today().isoformat()}."
        send_telegram_message(msg)
        return

    df = pd.DataFrame(data)

    # 4) Step 3: “Momentum sector” filter (≥ SECTOR_MIN_COUNT winners in the same industry)
    sector_counts = df['industry'].value_counts()
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
    df = df[df['industry'] == chosen_sector].copy()
    if df.empty:
        msg = f"⚠️ After sector filter, 0 stocks remain in {chosen_sector}."
        send_telegram_message(msg)
        return

    # 5) Step 4: OI filter ≥ OI_THRESHOLD
    df = df[df['oi_pct_change'] >= OI_THRESHOLD].copy()
    if df.empty:
        msg = (
            f"⚠️ No stocks in sector '{chosen_sector}' have OI Δ ≥ {OI_THRESHOLD:.1f}% at 09:30.\n"
            f"Date: {datetime.date.today().isoformat()}"
        )
        send_telegram_message(msg)
        return

    # 6) Sort by pct_change (descending) and pick top MAX_SYMBOLS_PER_DAY
    df = df.sort_values(by='pct_change', ascending=False).head(MAX_SYMBOLS_PER_DAY)

    # 7) Build the Markdown Telegram message
    header = f"🟢 *9:30 Intraday Picks for {datetime.date.today().isoformat()}*\n"
    header += f"Sector in focus: *{chosen_sector}*\n\n"

    lines = []
    for _, row in df.iterrows():
        sym   = row['symbol']
        cmp_p = row['cmp']
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
        "\n⚠️ *Remember:*  \n"
        " • Place a *Bracket‐Order* if your broker supports it (Groww, AngelOne, Dhan).  \n"
        " • If no BO, place market/limit buy → set SL at above SL.  \n"
        " • Move SL to breakeven at +1.5%; trail SL by –1% once +2% hits.  \n"
        " • *Exit all positions by 10:30 AM* IST if neither SL nor target is hit.  \n"
        " • Stop trading for the day if you lose 2 full SLs (~₹1,500–₹2,000)."
    )

    full_message = header + "\n".join(lines) + footer
    send_telegram_message(full_message)


if __name__ == "__main__":
    main()
