"""Replay Kronos on past hourly candles and save its forecasts next to what really happened.

Paper research only. Reads the database read-only and writes one CSV for the analysis notebook.
Run with the Kronos Python: D:\\Verify_Posts\\env_kronos\\Scripts\\python.exe research\\kronos_backtest_forecasts.py
"""
import sqlite3
import sys

import pandas as pd
import torch

sys.path.append(r"D:\Verify_Posts\src_kronos")
from model import Kronos, KronosPredictor, KronosTokenizer

DATABASE = "file:D:/Andy Trader/crypto_observations.db?mode=ro"
OUTPUT = r"D:\Andy Trader\research_data\kronos_backtest.csv"
INSTRUMENTS = ["BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD", "ADA-USD", "AVAX-USD", "LINK-USD"]
CONTEXT = 512
HORIZON = 24
STEP = 3
PATHS = 20

device = "cuda" if torch.cuda.is_available() else "cpu"
tokenizer = KronosTokenizer.from_pretrained("NeoQuasar/Kronos-Tokenizer-base")
model = Kronos.from_pretrained("NeoQuasar/Kronos-small")
predictor = KronosPredictor(model, tokenizer, device=device, max_context=CONTEXT)

connection = sqlite3.connect(DATABASE, uri=True)
rows = []
for instrument in INSTRUMENTS:
    candles = pd.read_sql_query(
        "select open_time, open, high, low, close, volume from crypto_observations "
        "where instrument = ? and venue = 'bybit' and interval = '1h' and degraded = 0 "
        "group by open_time order by open_time",
        connection,
        params=(instrument,),
    )
    candles["open_time"] = pd.to_datetime(candles["open_time"], utc=True, format="ISO8601").dt.tz_localize(None)
    # The database has one 476 hour hole in September; Kronos must only see unbroken hours.
    broken = candles["open_time"].diff() != pd.Timedelta(hours=1)
    broken.iloc[0] = False
    last_hole = broken[broken].index.max()
    if pd.notna(last_hole):
        candles = candles.iloc[last_hole:].reset_index(drop=True)
    for end in range(CONTEXT, len(candles) - HORIZON, STEP):
        window = candles.iloc[end - CONTEXT:end]
        x_timestamp = window["open_time"].reset_index(drop=True)
        y_timestamp = pd.Series(pd.date_range(x_timestamp.iloc[-1] + pd.Timedelta(hours=1), periods=HORIZON, freq="1h"))
        frame = window[["open", "high", "low", "close", "volume"]].reset_index(drop=True)
        paths = predictor.predict_batch(
            df_list=[frame] * PATHS,
            x_timestamp_list=[x_timestamp] * PATHS,
            y_timestamp_list=[y_timestamp] * PATHS,
            pred_len=HORIZON,
            T=1.0,
            top_p=0.9,
            sample_count=1,
            verbose=False,
        )
        closes = pd.DataFrame({i: p["close"].to_numpy() for i, p in enumerate(paths)})
        reference = float(window["close"].iloc[-1])
        future = candles["close"].iloc[end:end + HORIZON].to_numpy()
        rows.append({
            "instrument": instrument,
            "reference_time": x_timestamp.iloc[-1],
            "reference_price": reference,
            "share_up_4h": float((closes.iloc[3] > reference).mean()),
            "share_up_1d": float((closes.iloc[23] > reference).mean()),
            "actual_4h": float(future[3]),
            "actual_1d": float(future[23]),
        })
    print(instrument, "done", len(rows), "forecasts so far", flush=True)

pd.DataFrame(rows).to_csv(OUTPUT, index=False)
print("saved", OUTPUT)
