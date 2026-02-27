import pandas as pd
import numpy as np
import json
import os
import pickle
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from sklearn.ensemble import IsolationForest

LAX = ZoneInfo("America/Los_Angeles")

MACHINES_FILE   = "data/machines_log.csv"
MODEL_FILE      = "models/isolation_forest.pkl"
ALERTS_FILE     = "docs/alerts.json"
PICKUP_FILE     = "docs/pickup_rates.json"
MIN_ROWS        = 500  # ~3-4 days of data before training is meaningful

MODE_MAP = {"idle": 0, "running": 1, "pressStart": 2, "paused": 3}
CONTROLLER_MAP = {"ACA": 0, "QPRO": 1}
TYPE_MAP = {"washer": 0, "dryer": 1}

def engineer_features(df):
    df = df.sort_values(["opaque_id", "timestamp"]).copy()

    # Most important feature: how many consecutive polls in the same mode
    df["prev_mode"] = df.groupby("opaque_id")["mode"].shift(1)
    df["mode_changed"] = (df["mode"] != df["prev_mode"]).astype(int)
    df["mode_change_group"] = df.groupby("opaque_id")["mode_changed"].cumsum()
    df["consecutive_same_mode"] = df.groupby(
        ["opaque_id", "mode_change_group"]
    ).cumcount()

    # Approximate hours stuck in current mode (polls * 0.5 hours)
    df["hours_in_current_mode"] = df["consecutive_same_mode"] * 0.5

    # Encode categoricals for sklearn
    df["mode_encoded"]       = df["mode"].map(MODE_MAP).fillna(-1)
    df["controller_encoded"] = df["controller_type"].map(CONTROLLER_MAP).fillna(-1)
    df["type_encoded"]       = df["machine_type"].map(TYPE_MAP).fillna(-1)

    return df

FEATURE_COLS = [
    "available",
    "mode_encoded",
    "time_remaining",
    "door_closed",
    "in_service",
    "consecutive_same_mode",
    "hours_in_current_mode",
    "controller_encoded",
    "type_encoded",
    "hour_pst",
    "day_of_week",
    "can_add_time",
]

def explain(row):
    """Plain English explanation for community manager."""
    mode  = row["mode"]
    hours = float(row["hours_in_current_mode"])
    score = float(row["anomaly_score"])

    if mode == "running" and hours >= 2.0:
        return f"Running continuously for {hours:.1f}h — normal max is ~1h. May be stuck."
    if mode == "pressStart" and hours >= 1.5:
        return f"Waiting to start for {hours:.1f}h — cycle was likely interrupted."
    if row["available"] == 0 and row["in_service"] == 1:
        return "Marked out of service by CSC maintenance system."
    if mode == "running" and row["time_remaining"] == 0 and hours >= 1.0:
        return f"Shows running but time remaining is 0 for {hours:.1f}h — sensor may be stuck."
    if row["door_closed"] == 0 and mode == "running":
        return "Running with door open — unusual state."
    return f"Unusual combination of states (score: {score:.2f}). Worth a visual check."

def read_machines_csv(path):
    """Read machines_log.csv; tolerate 26-col header with some 27-col rows (poll_type added mid-run)."""
    import csv
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        rows = []
        for row in reader:
            if len(row) == len(header) + 1 and len(header) == 26:
                row = row[:4] + row[5:]  # drop extra 5th column (poll_type)
            if len(row) == len(header):
                rows.append(row)
    df = pd.DataFrame(rows, columns=header)
    # Coerce numeric columns (all come in as str from csv.reader)
    for col in ("hour_pst", "minute", "day_of_week", "sticker", "available", "time_remaining",
                "door_closed", "in_service", "free_play", "can_add_time", "show_settings"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)
    return df


def main():
    if not os.path.isfile(MACHINES_FILE):
        print("No machines_log.csv found — skipping.")
        write_empty_alerts("No machine data collected yet.")
        return

    df = read_machines_csv(MACHINES_FILE)
    if "poll_type" in df.columns:
        df = df[df["poll_type"].fillna("scheduled") == "scheduled"]
    total_rows = len(df)

    write_pickup_rates(df)

    if total_rows < MIN_ROWS:
        remaining = MIN_ROWS - total_rows
        print(f"Only {total_rows} rows collected, need {MIN_ROWS}. {remaining} more to go.")
        write_empty_alerts(
            f"Collecting data — {total_rows}/{MIN_ROWS} rows. "
            f"Check back in ~{round(remaining / 48)} more day(s)."
        )
        return

    df = engineer_features(df)
    X  = df[FEATURE_COLS].fillna(0)

    # Train Isolation Forest
    model = IsolationForest(
        n_estimators=200,
        contamination=0.05,
        random_state=42,
        n_jobs=-1
    )
    model.fit(X)
    df["raw_score"] = model.decision_function(X)

    # Normalize: higher score = more anomalous (0 to 1)
    inv = -df["raw_score"]
    df["anomaly_score"] = (inv - inv.min()) / (inv.max() - inv.min() + 1e-9)

    # Get most recent row per machine (current state)
    latest = (
        df.sort_values("timestamp")
        .groupby("opaque_id")
        .last()
        .reset_index()
    )

    machines_out = []
    for _, row in latest.iterrows():
        score = float(row["anomaly_score"])

        if score > 0.75:
            status = "critical"
        elif score > 0.55:
            status = "warning"
        elif score > 0.35:
            status = "watch"
        else:
            status = "normal"

        machines_out.append({
            "sticker":               int(row["sticker"]),
            "machine_type":          str(row["machine_type"]),
            "controller_type":       str(row["controller_type"]),
            "current_mode":          str(row["mode"]),
            "available":             bool(row["available"]),
            "time_remaining":        int(row["time_remaining"]),
            "door_closed":           bool(row["door_closed"]),
            "in_service":            bool(row["in_service"]),
            "hours_in_current_mode": round(float(row["hours_in_current_mode"]), 1),
            "anomaly_score":         round(score, 3),
            "status":                status,
            "reason":                explain(row) if status != "normal" else "Operating normally",
            "last_seen":             str(row["timestamp"]),
        })

    # Sort: critical → warning → watch → normal, then by sticker number
    order = {"critical": 0, "warning": 1, "watch": 2, "normal": 3}
    machines_out.sort(key=lambda x: (order[x["status"]], x["sticker"]))

    alerts = {
        "trained_at":   datetime.now(LAX).strftime("%Y-%m-%d %H:%M:%S"),
        "total_rows":   total_rows,
        "model_status": "ok",
        "message":      None,
        "machines":     machines_out,
    }

    os.makedirs("docs", exist_ok=True)
    with open(ALERTS_FILE, "w") as f:
        json.dump(alerts, f, indent=2)

    os.makedirs("models", exist_ok=True)
    with open(MODEL_FILE, "wb") as f:
        pickle.dump(model, f)

    flagged = [m for m in machines_out if m["status"] in ("critical", "warning")]
    print(f"Done. Trained on {total_rows} rows. {len(flagged)} machine(s) flagged.")
    for m in flagged:
        print(f"  [{m['status'].upper()}] {m['machine_type']} #{m['sticker']}: {m['reason']}")


def write_empty_alerts(message):
    os.makedirs("docs", exist_ok=True)
    with open(ALERTS_FILE, "w") as f:
        json.dump({
            "trained_at":   datetime.now(LAX).strftime("%Y-%m-%d %H:%M:%S"),
            "model_status": "accumulating",
            "message":      message,
            "machines":     [],
        }, f, indent=2)


def compute_pickup_rates(df):
    """From scheduled washer rows: running → available transitions; next poll door_closed = stranded."""
    washers = df[df["machine_type"] == "washer"].copy()
    if washers.empty:
        return {}
    washers = washers.sort_values(["opaque_id", "timestamp"])
    # events: (day_of_week, hour_pst, stranded)
    events = []
    for opaque_id, grp in washers.groupby("opaque_id"):
        grp = grp.reset_index(drop=True)
        for i in range(1, len(grp)):
            prev = grp.iloc[i - 1]
            curr = grp.iloc[i]
            if prev["mode"] == "running" and curr["available"] == 1:
                stranded = 1 if curr["door_closed"] == 1 else 0
                events.append((int(curr["day_of_week"]), int(curr["hour_pst"]), stranded))
    if not events:
        return {}
    by_dow_hour = {}
    for d, h, stranded in events:
        by_dow_hour.setdefault(d, {}).setdefault(h, {"stranded": 0, "total": 0})
        by_dow_hour[d][h]["total"] += 1
        by_dow_hour[d][h]["stranded"] += stranded
    washer_stranded_rate = {}
    for d, hours in by_dow_hour.items():
        washer_stranded_rate[str(d)] = {
            str(h): round(data["stranded"] / data["total"], 2)
            for h, data in hours.items()
        }
    return washer_stranded_rate


def write_pickup_rates(df):
    rates = compute_pickup_rates(df)
    os.makedirs("docs", exist_ok=True)
    with open(PICKUP_FILE, "w") as f:
        json.dump({
            "generated_at":         datetime.now(LAX).strftime("%Y-%m-%d %H:%M:%S"),
            "washer_stranded_rate": rates,
        }, f, indent=2)


if __name__ == "__main__":
    main()
