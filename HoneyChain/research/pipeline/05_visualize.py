"""
Step 9: Visualization

Generates the full set of professional plots requested, saved as PNG files
into research/outputs/plots/. Abnormal periods are highlighted in red.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _paths import DATA_DIR, MODEL_DIR, OUTPUT_DIR, data, model_path  # repository-root-relative paths
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

OUT_DIR = str(OUTPUT_DIR)
plt.rcParams["figure.figsize"] = (13, 5)
plt.rcParams["axes.grid"] = True
plt.rcParams["grid.alpha"] = 0.3


def load_data():
    df = pd.read_csv(data("hive_health_scored.csv"), parse_dates=["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def highlight_abnormal(ax, df):
    abnormal = df[df["final_status"].isin(["WARNING", "HIGH RISK"])]
    for t in abnormal["timestamp"]:
        ax.axvspan(t, t + pd.Timedelta(minutes=1), color="red", alpha=0.15)


def plot_timeseries(df, col, title, ylabel, fname, color="tab:blue"):
    fig, ax = plt.subplots()
    ax.plot(df["timestamp"], df[col], color=color, linewidth=1)
    highlight_abnormal(ax, df)
    ax.set_title(title)
    ax.set_xlabel("Time")
    ax.set_ylabel(ylabel)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(f"{OUT_DIR}/{fname}", dpi=130)
    plt.close(fig)


def plot_dual(df, col_in, col_out, title, ylabel, fname):
    fig, ax = plt.subplots()
    ax.plot(df["timestamp"], df[col_in], label="Inside", color="tab:red")
    ax.plot(df["timestamp"], df[col_out], label="Outside", color="tab:blue")
    highlight_abnormal(ax, df)
    ax.set_title(title)
    ax.set_xlabel("Time")
    ax.set_ylabel(ylabel)
    ax.legend()
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(f"{OUT_DIR}/{fname}", dpi=130)
    plt.close(fig)


def plot_scatter(df, xcol, ycol, title, fname):
    fig, ax = plt.subplots(figsize=(6, 6))
    ax.scatter(df[xcol], df[ycol], s=6, alpha=0.4, c=df["health_score"], cmap="RdYlGn")
    ax.set_xlabel(xcol)
    ax.set_ylabel(ycol)
    ax.set_title(title)
    fig.tight_layout()
    fig.savefig(f"{OUT_DIR}/{fname}", dpi=130)
    plt.close(fig)


def plot_status_timeline(df, fname):
    status_map = {"HEALTHY": 3, "NORMAL - MONITOR": 2,
                  "NORMAL - MONITOR (transient anomaly)": 2,
                  "WARNING": 1, "HIGH RISK": 0}
    colors = {3: "green", 2: "gold", 1: "orange", 0: "red"}
    y = df["final_status"].map(status_map)
    fig, ax = plt.subplots()
    ax.scatter(df["timestamp"], y, c=[colors[v] for v in y], s=8)
    ax.set_yticks([0, 1, 2, 3])
    ax.set_yticklabels(["HIGH RISK", "WARNING", "NORMAL/MONITOR", "HEALTHY"])
    ax.set_title("Risk Classification Over Time")
    ax.set_xlabel("Time")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(f"{OUT_DIR}/{fname}", dpi=130)
    plt.close(fig)


def main():
    import os
    os.makedirs(OUT_DIR, exist_ok=True)
    df = load_data()

    plot_timeseries(df, "inside_temperature", "Inside Hive Temperature vs Time",
                     "Temperature (C)", "01_inside_temperature.png", "tab:red")
    plot_timeseries(df, "outside_temperature", "Outside Temperature vs Time",
                     "Temperature (C)", "02_outside_temperature.png", "tab:blue")
    plot_timeseries(df, "inside_humidity", "Inside Hive Humidity vs Time",
                     "Humidity (%)", "03_inside_humidity.png", "tab:red")
    plot_timeseries(df, "outside_humidity", "Outside Humidity vs Time",
                     "Humidity (%)", "04_outside_humidity.png", "tab:blue")

    plot_dual(df, "inside_temperature", "outside_temperature",
              "Inside vs Outside Temperature", "Temperature (C)", "05_temp_inside_vs_outside.png")
    plot_dual(df, "inside_humidity", "outside_humidity",
              "Inside vs Outside Humidity", "Humidity (%)", "06_humidity_inside_vs_outside.png")

    plot_timeseries(df, "temperature_difference", "Temperature Difference (Inside - Outside) Over Time",
                     "Delta T (C)", "07_temperature_difference.png", "purple")
    plot_timeseries(df, "humidity_difference", "Humidity Difference (Inside - Outside) Over Time",
                     "Delta RH (%)", "08_humidity_difference.png", "purple")

    plot_timeseries(df, "iso_forest_score", "Anomaly Score Over Time (Isolation Forest)",
                     "Anomaly score (higher = more normal)", "09_anomaly_score.png", "black")
    plot_timeseries(df, "health_score", "Hive Health Score Over Time (0-100)",
                     "Health Score", "10_health_score.png", "green")

    plot_status_timeline(df, "11_risk_classification_timeline.png")

    print(f"Saved {len(os.listdir(OUT_DIR))} plots to {OUT_DIR}/")


if __name__ == "__main__":
    main()
