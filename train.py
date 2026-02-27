import pandas as pd
import numpy as np
import json
import os
import pickle
from datetime import datetime
from zoneinfo import ZoneInfo
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import StratifiedKFold, cross_validate
from sklearn.metrics import classification_report

LAX = ZoneInfo("America/Los_Angeles")

MACHINES_FILE  = "data/machines_log.csv"
MODEL_FILE     = "models/random_forest.pkl"
ALERTS_FILE    = "docs/alerts.json"
PICKUP_FILE    = "docs/pickup_rates.json"
METRICS_FILE   = "docs/model_metrics.json"
MIN_ROWS       = 500  # ~3-4 days of data before training is meaningful

# Precision-tuned thresholds (see docs/model_metrics.json for audit trail)
WATCH_THRESHOLD    = 0.50
WARNING_THRESHOLD  = 0.70
CRITICAL_THRESHOLD = 0.85
CONFIRMATION_POLLS = 2  # warning/critical require this many consecutive polls above WARNING_THRESHOLD

MODE_MAP       = {"idle": 0, "running": 1, "pressStart": 2, "paused": 3, "offline": 4}
CONTROLLER_MAP = {"ACA": 0, "QPRO": 1}
TYPE_MAP       = {"washer": 0, "dryer": 1}


def engineer_features(df):
    df = df.sort_values(["opaque_id", "timestamp"]).copy()

    df["prev_mode"] = df.groupby("opaque_id")["mode"].shift(1)
    df["mode_changed"] = (df["mode"] != df["prev_mode"]).astype(int)
    df["mode_change_group"] = df.groupby("opaque_id")["mode_changed"].cumsum()
    df["consecutive_same_mode"] = df.groupby(
        ["opaque_id", "mode_change_group"]
    ).cumcount()

    # Hours stuck in current mode (each poll ≈ 30 min = 0.5h)
    df["hours_in_current_mode"] = df["consecutive_same_mode"] * 0.5

    # Rolling 12-poll (6h) window: how often was this machine missing?
    df["was_missing"] = df.get("was_missing", pd.Series(0, index=df.index)).fillna(0).astype(int)
    df["missing_rate_6h"] = (
        df.groupby("opaque_id")["was_missing"]
        .transform(lambda x: x.shift(1).rolling(12, min_periods=1).mean())
        .fillna(0)
    )

    # How often has the machine been flapping (appearing/disappearing)?
    df["prev_missing"] = df.groupby("opaque_id")["was_missing"].shift(1).fillna(0)
    df["state_flap"] = (df["was_missing"] != df["prev_missing"]).astype(int)
    df["flap_rate_6h"] = (
        df.groupby("opaque_id")["state_flap"]
        .transform(lambda x: x.shift(1).rolling(12, min_periods=1).mean())
        .fillna(0)
    )

    df["mode_encoded"]       = df["mode"].map(MODE_MAP).fillna(-1).astype(int)
    df["controller_encoded"] = df["controller_type"].map(CONTROLLER_MAP).fillna(-1).astype(int)
    df["type_encoded"]       = df["machine_type"].map(TYPE_MAP).fillna(-1).astype(int)
    df["has_unavail_reason"] = (df["not_available_reason"].fillna("").str.len() > 0).astype(int)

    # Is the machine actively cycling? Running with timer > 0 means the cycle is progressing
    # normally — the timer is counting down. This is the clearest signal that a running machine
    # is NOT stuck.
    df["is_active_cycle"] = (
        (df["mode"] == "running") &
        (df["time_remaining"].astype(float) > 0)
    ).astype(int)

    return df


# ✅ Behavioral signals, encoded categories, engineered temporal features
# ❌ Excluded: opaque_id, sticker, license_plate, nfc_id, qr_code_id, timestamp,
#    display, group_id, soil, cycle, washer_temp, dryer_temp, show_settings, minute, poll_type
# ❌ can_add_time removed: hardware constant per machine type — 12.35% importance was
#    machine-identity leak, not a behavioral signal
FEATURE_COLS = [
    "available",
    "mode_encoded",
    "time_remaining",
    "is_active_cycle",      # 1 if running with timer > 0 (cycle is progressing, not stuck)
    "door_closed",
    "in_service",
    "has_unavail_reason",
    "free_play",
    "consecutive_same_mode",
    "hours_in_current_mode",
    "missing_rate_6h",
    "flap_rate_6h",
    "type_encoded",
    "controller_encoded",
    "hour_pst",
    "day_of_week",
]


def generate_labels(df):
    """
    Supervised binary labels: 0 = normal, 1 = anomaly.
    Derived from known ground truth, not statistical inference.

    Priority order (higher wins):
      1. was_missing         — absent from API (highest confidence)
      2. stuck running       — running > 2h AND timer = 0 (timer>0 means cycle still progressing)
      3. pressStart stuck    — waiting > 1.5h (interrupted cycle)
      4. running, timer = 0  — sensor may be stuck
    """
    labels = pd.Series(0, index=df.index)

    if "was_missing" in df.columns:
        labels[df["was_missing"] == 1] = 1

    stuck_running = (
        (df["mode"] == "running") &
        (df["hours_in_current_mode"] >= 2.0) &
        (df["time_remaining"].astype(float) == 0)  # timer must be 0 — if timer>0 the cycle is still progressing
    )
    labels[stuck_running] = 1

    stuck_press_start = (
        (df["mode"] == "pressStart") &
        (df["hours_in_current_mode"] >= 1.5)
    )
    labels[stuck_press_start] = 1

    running_no_timer = (
        (df["mode"] == "running") &
        (df["time_remaining"] == 0) &
        (df["hours_in_current_mode"] >= 1.0)
    )
    labels[running_no_timer] = 1

    return labels


def explain(row):
    """Plain-English explanation for community manager."""
    if row.get("was_missing", 0) == 1:
        return "Machine absent from API response — not communicating with CSC system."
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
    """Read machines_log.csv; tolerates 26-col header with some 27-col rows (poll_type added mid-run)."""
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
    for col in ("hour_pst", "minute", "day_of_week", "sticker", "available", "time_remaining",
                "door_closed", "in_service", "free_play", "can_add_time", "show_settings",
                "was_missing"):
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
    X = df[FEATURE_COLS].fillna(0)

    df["label"] = generate_labels(df)
    y = df["label"]

    n_anomaly = int(y.sum())
    n_normal  = int((y == 0).sum())
    print(f"Label distribution: {n_normal} normal, {n_anomaly} anomaly rows")

    if n_anomaly < 5:
        print("Not enough anomaly labels yet for supervised training — need at least 5.")
        print("Tip: Check that manifest has stabilized and was_missing rows are being logged.")
        write_empty_alerts("Collecting anomaly labels — need more offline/stuck events.")
        return

    # class_weight={0:1, 1:5}: manual balance trades some recall for much higher precision
    # vs "balanced" (~27x upweight) which caused false positives on ghost-running washers
    model = RandomForestClassifier(
        n_estimators=200,
        max_depth=8,
        min_samples_leaf=5,
        class_weight={0: 1, 1: 5},
        random_state=42,
        n_jobs=-1,
    )

    cv_metrics = {}
    if n_anomaly >= 10:
        cv = StratifiedKFold(n_splits=min(5, n_anomaly), shuffle=True, random_state=42)
        cv_results = cross_validate(
            model, X, y, cv=cv,
            scoring=["precision_macro", "recall_macro", "f1_macro", "average_precision"],
            error_score="raise"
        )
        cv_metrics = {
            "cv_precision_macro":    round(float(cv_results["test_precision_macro"].mean()), 3),
            "cv_recall_macro":       round(float(cv_results["test_recall_macro"].mean()), 3),
            "cv_f1_macro":           round(float(cv_results["test_f1_macro"].mean()), 3),
            "cv_average_precision":  round(float(cv_results["test_average_precision"].mean()), 3),
        }
        print(f"CV Precision: {cv_metrics['cv_precision_macro']:.2f}  "
              f"Recall: {cv_metrics['cv_recall_macro']:.2f}  "
              f"F1: {cv_metrics['cv_f1_macro']:.2f}  "
              f"AvgPrec: {cv_metrics['cv_average_precision']:.2f}")

    model.fit(X, y)
    df["anomaly_score"] = model.predict_proba(X)[:, 1]

    df_sorted = df.sort_values("timestamp")
    latest = df_sorted.groupby("opaque_id").last().reset_index()

    # Second-to-last row per machine for consecutive-poll confirmation gate (RC4)
    # .nth(-2) keeps opaque_id as index; reset_index() promotes it back to a column
    second_latest = df_sorted.groupby("opaque_id").nth(-2).reset_index()
    if "opaque_id" not in second_latest.columns:
        # Fallback: only 1 poll per machine — use the same row as latest
        second_latest = latest.copy()
    second_latest_scores = model.predict_proba(second_latest[FEATURE_COLS].fillna(0))[:, 1]
    prev_score_by_id = dict(zip(second_latest["opaque_id"], second_latest_scores))

    machines_out = []
    for _, row in latest.iterrows():
        score = float(row["anomaly_score"])
        oid   = str(row["opaque_id"])

        if row.get("was_missing", 0) == 1:
            status = "offline"
        elif str(row["mode"]) == "running" and int(row.get("time_remaining", 0)) > 0:
            # Machine has an active cycle with time remaining — the timer is counting down,
            # so it is definitively NOT stuck. Hard-bypass ML to prevent false positives.
            status = "normal"
        else:
            # Consecutive-poll confirmation: warning/critical require previous poll
            # also above WARNING_THRESHOLD to prevent single-poll noise spikes
            prev_score  = float(prev_score_by_id.get(oid, 0.0))
            confirmed   = prev_score >= WARNING_THRESHOLD

            if score >= CRITICAL_THRESHOLD and confirmed:
                status = "critical"
            elif score >= WARNING_THRESHOLD and confirmed:
                status = "warning"
            elif score >= WATCH_THRESHOLD:
                # watch needs no confirmation — single poll is enough for a soft signal
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
            "was_missing":           int(row.get("was_missing", 0)),
            "status":                status,
            "reason":                explain(row) if status != "normal" else "Operating normally",
            "last_seen":             str(row["timestamp"]),
        })

    order = {"offline": 0, "critical": 1, "warning": 2, "watch": 3, "normal": 4}
    machines_out.sort(key=lambda x: (order[x["status"]], x["sticker"]))

    trained_at = datetime.now(LAX).strftime("%Y-%m-%d %H:%M:%S")

    alerts = {
        "trained_at":         trained_at,
        "total_rows":         total_rows,
        "model_status":       "ok",
        "model_type":         "RandomForestClassifier",
        "n_anomaly_labels":   n_anomaly,
        "n_normal_labels":    n_normal,
        "feature_cols":       FEATURE_COLS,
        "message":            None,
        "machines":           machines_out,
    }

    metrics = {
        "trained_at":                   trained_at,
        "total_rows":                   total_rows,
        "n_anomaly_labels":             n_anomaly,
        "n_normal_labels":              n_normal,
        "precision_target":             "high_precision_low_fp",
        "class_weight":                 "0:1, 1:5",
        "watch_threshold":              WATCH_THRESHOLD,
        "warning_threshold":            WARNING_THRESHOLD,
        "critical_threshold":           CRITICAL_THRESHOLD,
        "confirmation_required_polls":  CONFIRMATION_POLLS,
        "feature_cols":                 FEATURE_COLS,
        **cv_metrics,
    }

    os.makedirs("docs", exist_ok=True)
    with open(ALERTS_FILE, "w") as f:
        json.dump(alerts, f, indent=2)
    with open(METRICS_FILE, "w") as f:
        json.dump(metrics, f, indent=2)

    os.makedirs("models", exist_ok=True)
    with open(MODEL_FILE, "wb") as f:
        pickle.dump(model, f)

    flagged = [m for m in machines_out if m["status"] in ("offline", "critical", "warning")]
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
