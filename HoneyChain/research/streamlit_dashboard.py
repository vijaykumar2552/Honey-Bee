"""
Step 13: HoneyChain Streamlit Dashboard

Run with:
    streamlit run app.py

Displays current sensor readings, hive health score, risk level, historical
graphs, alerts, and abnormality explanations. Supports selecting between
multiple hives (Hive_001, Hive_002, ...).
"""
import sys
import os
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pipeline"))

import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go

st.set_page_config(page_title="HoneyChain - Hive Health Dashboard", layout="wide")

DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "hive_health_scored.csv")

STATUS_COLOR = {
    "HEALTHY": "🟢",
    "NORMAL - MONITOR": "🟡",
    "NORMAL - MONITOR (transient anomaly)": "🟡",
    "WARNING": "🟠",
    "HIGH RISK": "🔴",
}


@st.cache_data
def load_data():
    df = pd.read_csv(DATA_PATH, parse_dates=["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)
    # Simulate a Hive_ID column since our current dataset is single-hive;
    # this is the plug point for genuinely multi-hive data later.
    df["Hive_ID"] = "Hive_001"
    return df


def main():
    st.title("🐝 HoneyChain — AI Hive Health & Disease-Risk Dashboard")
    st.caption(
        "AI-Based Early Honeybee Hive Health and Disease-Risk Detection Using "
        "Inside and Outside Environmental Monitoring"
    )

    df = load_data()

    # ---------------- Sidebar: Hive selector ----------------
    hive_ids = sorted(df["Hive_ID"].unique())
    selected_hive = st.sidebar.selectbox("Select Hive", hive_ids)
    hive_df = df[df["Hive_ID"] == selected_hive].reset_index(drop=True)

    st.sidebar.markdown("---")
    st.sidebar.info(
        "⚠️ **Scientific limitation**: Temperature and humidity are "
        "environmental indicators and cannot independently confirm a "
        "specific honeybee disease. This system detects abnormal "
        "environmental/health patterns and screens for disease *risk* — "
        "confirmatory diagnosis requires colony inspection."
    )

    latest = hive_df.iloc[-1]

    # ---------------- Top status banner ----------------
    status_icon = STATUS_COLOR.get(latest["final_status"], "⚪")
    st.markdown(f"## {status_icon} Current Status: **{latest['final_status']}**  "
                f"(Hive Health Score: {latest['health_score']:.0f}/100)")

    # ---------------- Current readings ----------------
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Inside Temperature", f"{latest['inside_temperature']:.1f} °C")
    c2.metric("Inside Humidity", f"{latest['inside_humidity']:.0f} %")
    c3.metric("Outside Temperature", f"{latest['outside_temperature']:.1f} °C")
    c4.metric("Outside Humidity", f"{latest['outside_humidity']:.0f} %")

    c5, c6, c7 = st.columns(3)
    c5.metric("Hive Health Score", f"{latest['health_score']:.0f} / 100")
    c6.metric("Risk Level", latest["final_risk_level"])
    c7.metric("Anomaly Score", f"{latest['iso_forest_score']:.3f}")

    # ---------------- Explanation ----------------
    st.subheader("🔍 Why this classification?")
    reasons = str(latest["reasons"]).split(" | ")
    for r in reasons:
        st.write(f"- {r}")

    # ---------------- Alerts ----------------
    st.subheader("🚨 Recent Alerts")
    alerts = hive_df[hive_df["final_status"].isin(["WARNING", "HIGH RISK"])].tail(10)
    if len(alerts) == 0:
        st.success("No recent WARNING/HIGH RISK alerts.")
    else:
        st.dataframe(
            alerts[["timestamp", "final_status", "final_risk_level", "health_score", "reasons"]]
            .rename(columns={"final_status": "Status", "final_risk_level": "Risk",
                              "health_score": "Score", "reasons": "Reason"}),
            use_container_width=True,
        )

    # ---------------- Historical graphs ----------------
    st.subheader("📈 Historical Environmental Data")
    tab1, tab2, tab3 = st.tabs(["Temperature", "Humidity", "Health Score & Risk"])

    with tab1:
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=hive_df["timestamp"], y=hive_df["inside_temperature"],
                                  name="Inside Temp", line=dict(color="red")))
        fig.add_trace(go.Scatter(x=hive_df["timestamp"], y=hive_df["outside_temperature"],
                                  name="Outside Temp", line=dict(color="blue")))
        fig.update_layout(height=400, xaxis_title="Time", yaxis_title="Temperature (°C)")
        st.plotly_chart(fig, use_container_width=True)

    with tab2:
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=hive_df["timestamp"], y=hive_df["inside_humidity"],
                                  name="Inside Humidity", line=dict(color="red")))
        fig.add_trace(go.Scatter(x=hive_df["timestamp"], y=hive_df["outside_humidity"],
                                  name="Outside Humidity", line=dict(color="blue")))
        fig.update_layout(height=400, xaxis_title="Time", yaxis_title="Humidity (%)")
        st.plotly_chart(fig, use_container_width=True)

    with tab3:
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=hive_df["timestamp"], y=hive_df["health_score"],
                                  name="Health Score", line=dict(color="green")))
        # Shade risk zones
        fig.add_hrect(y0=80, y1=100, fillcolor="green", opacity=0.08, line_width=0)
        fig.add_hrect(y0=60, y1=80, fillcolor="yellow", opacity=0.08, line_width=0)
        fig.add_hrect(y0=40, y1=60, fillcolor="orange", opacity=0.08, line_width=0)
        fig.add_hrect(y0=0, y1=40, fillcolor="red", opacity=0.08, line_width=0)
        fig.update_layout(height=400, xaxis_title="Time", yaxis_title="Health Score (0-100)")
        st.plotly_chart(fig, use_container_width=True)

    st.markdown("---")
    st.caption(
        "HoneyChain Prototype — AI-IoT Based Smart Beekeeping, Honey Traceability "
        "and Disease Risk Detection System. Environmental health screening module."
    )


if __name__ == "__main__":
    main()
