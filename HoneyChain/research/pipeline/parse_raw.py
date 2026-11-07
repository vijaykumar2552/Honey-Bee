"""
Parses the raw hive sensor text (as extracted from the user's PDF/Excel export)
into a clean CSV. The raw text has rows like:

03 Sept 2026 12:02:10 pm 34.4 72 30.7 60

with occasional glued rows (two records on one line with no space between them,
an artifact of PDF text extraction, e.g. "...60 60 03 Sept 2026 12:03:00 pm...").

This script uses a regex to robustly re-split the stream into individual
records regardless of line breaks.
"""
import re
import pandas as pd

RAW_PATH = "raw_data.txt"
OUT_PATH = "hive_environment_raw.csv"

with open(RAW_PATH, "r") as f:
    text = f.read()

# Normalize whitespace/newlines into a single stream, then re-tokenize.
text = re.sub(r"\s+", " ", text)

# Pattern for one record:
# DD Mon YYYY HH:MM:SS am/pm  temp_in  hum_in  temp_out  hum_out
pattern = re.compile(
    r"(\d{2}\s+Sept\s+2026\s+\d{2}:\d{2}:\d{2}\s+[ap]m)\s+"
    r"([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)"
)

records = []
for m in pattern.finditer(text):
    ts_str, t_in, h_in, t_out, h_out = m.groups()
    records.append({
        "timestamp_str": ts_str,
        "inside_temperature": float(t_in),
        "inside_humidity": float(h_in),
        "outside_temperature": float(t_out),
        "outside_humidity": float(h_out),
    })

df = pd.DataFrame(records)
print(f"Parsed {len(df)} records")

# Parse timestamps: format "03 Sept 2026 12:02:10 pm"
df["timestamp"] = pd.to_datetime(df["timestamp_str"], format="%d %b %Y %I:%M:%S %p", errors="coerce")
# "Sept" is nonstandard for %b (which expects "Sep"); handle manually
mask = df["timestamp"].isna()
if mask.any():
    df.loc[mask, "timestamp"] = pd.to_datetime(
        df.loc[mask, "timestamp_str"].str.replace("Sept", "Sep", regex=False),
        format="%d %b %Y %I:%M:%S %p", errors="coerce"
    )

n_bad = df["timestamp"].isna().sum()
print(f"Unparseable timestamps: {n_bad}")
df = df.dropna(subset=["timestamp"]).drop(columns=["timestamp_str"])
df = df.sort_values("timestamp").reset_index(drop=True)

df.to_csv(OUT_PATH, index=False)
print(f"Saved to {OUT_PATH}")
print(df.head())
print(df.tail())
print(f"Time range: {df['timestamp'].min()} to {df['timestamp'].max()}")
