import requests
import csv
import json
import os
from datetime import datetime, timezone

LOCATION_ID = os.environ["LOCATION_ID"]
ROOM_ID     = os.environ["ROOM_ID"]
DATA_FILE   = "data/laundry_log.csv"
SUMMARY_FILE = "docs/summary.json"

def get_machines():
    url = f"https://mycscgo.com/api/v3/location/{LOCATION_ID}/room/{ROOM_ID}/machines"
    return requests.get(url, timeout=10).json()

def main():
    now       = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
    hour_pst  = (now.hour - 8) % 24

    machines = get_machines()
    washers  = [m for m in machines if m["type"] == "washer"]
    dryers   = [m for m in machines if m["type"] == "dryer"]

    row = {
        "timestamp":      timestamp,
        "hour_pst":       hour_pst,
        "washers_free":   sum(1 for m in washers if m["available"]),
        "washers_in_use": sum(1 for m in washers if not m["available"]),
        "washers_total":  len(washers),
        "dryers_free":    sum(1 for m in dryers  if m["available"]),
        "dryers_in_use":  sum(1 for m in dryers  if not m["available"]),
        "dryers_total":   len(dryers),
        "all_free":       sum(1 for m in machines if m["available"]),
        "all_in_use":     sum(1 for m in machines if not m["available"]),
        "all_total":      len(machines),
    }

    # Append to private CSV log
    os.makedirs("data", exist_ok=True)
    file_exists = os.path.isfile(DATA_FILE)
    with open(DATA_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=row.keys())
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)

    # Read last 168 rows (7 days) for history — NO location IDs written here
    history = []
    if os.path.isfile(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            reader = list(csv.DictReader(f))
            for r in reader[-168:]:
                history.append({
                    "timestamp":      r["timestamp"],
                    "hour_pst":       int(r["hour_pst"]),
                    "washers_free":   int(r["washers_free"]),
                    "washers_in_use": int(r["washers_in_use"]),
                    "dryers_free":    int(r["dryers_free"]),
                    "dryers_in_use":  int(r["dryers_in_use"]),
                    "all_free":       int(r["all_free"]),
                    "all_in_use":     int(r["all_in_use"]),
                    "all_total":      int(r["all_total"]),
                })

    # Write sanitized public summary — safe to expose
    os.makedirs("docs", exist_ok=True)
    summary = {
        "last_updated":   timestamp,
        "current": {
            "washers_free":   row["washers_free"],
            "washers_in_use": row["washers_in_use"],
            "washers_total":  row["washers_total"],
            "dryers_free":    row["dryers_free"],
            "dryers_in_use":  row["dryers_in_use"],
            "dryers_total":   row["dryers_total"],
            "all_free":       row["all_free"],
            "all_in_use":     row["all_in_use"],
            "all_total":      row["all_total"],
        },
        "history": history
    }
    with open(SUMMARY_FILE, "w") as f:
        json.dump(summary, f, indent=2)

    print(f"[{timestamp} | {hour_pst}:00 PST] "
          f"Washers: {row['washers_in_use']}/{row['washers_total']} in use | "
          f"Dryers: {row['dryers_in_use']}/{row['dryers_total']} in use")

if __name__ == "__main__":
    main()
