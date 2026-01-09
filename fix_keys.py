import json
import pandas as pd
import re

# Input files
JSON_FILE = "NSE.json"
CSV_FILE = "stocks_symbols.csv"
OUTPUT = "symbols_clean.csv"

# Load CSV (your 200 stocks)
df = pd.read_csv(CSV_FILE)
df["tradingsymbol"] = df["tradingsymbol"].str.upper().str.strip()

# Load full Upstox instrument file
with open(JSON_FILE, "r", encoding="utf-8") as f:
    data = json.load(f)

# Filter: Only NSE_EQ equities with VALID numeric instrument_key
valid_eq = []
for item in data:
    if (
        item.get("segment") == "NSE_EQ"
        and item.get("instrument_type") == "EQ"
        and re.match(r"^NSE_EQ\|\d+$", item.get("instrument_key", ""))  # numeric key only
    ):
        valid_eq.append({
            "instrument_key": item["instrument_key"],
            "tradingsymbol": item["trading_symbol"].upper(),
            "exchange_token": item.get("exchange_token", "")
        })

valid_df = pd.DataFrame(valid_eq)

# Merge your CSV stocks with valid numeric-upstox symbols
merged = df.merge(valid_df, on="tradingsymbol", how="inner")

# Save only valid matches
merged.to_csv(OUTPUT, index=False)

print("--------------------------------------------------")
print(f"INPUT STOCKS : {len(df)}")
print(f"VALID MATCHES: {len(merged)}")
print(f"SAVED CLEAN FILE: {OUTPUT}")
print("--------------------------------------------------")

missing = df[~df["tradingsymbol"].isin(merged["tradingsymbol"])]
if not missing.empty:
    print("\nStocks WITHOUT valid instrument_key:")
    print(missing["tradingsymbol"].tolist())
