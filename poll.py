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

    washer_total = len(washers)
    dryer_total  = len(dryers)
    all_total    = len(machines)
    washers_in_use = sum(1 for m in washers if not m["available"])
    dryers_in_use  = sum(1 for m in dryers  if not m["available"])
    all_in_use     = sum(1 for m in machines if not m["available"])

    row = {
        "timestamp":           timestamp,
        "hour_pst":            hour_pst,
        "day_of_week":         now.weekday(),  # 0=Mon, 6=Sun
        "washers_free":        sum(1 for m in washers if m["available"]),
        "washers_in_use":      washers_in_use,
        "washers_total":       washer_total,
        "dryers_free":         sum(1 for m in dryers if m["available"]),
        "dryers_in_use":       dryers_in_use,
        "dryers_total":        dryer_total,
        "all_free":            sum(1 for m in machines if m["available"]),
        "all_in_use":          all_in_use,
        "all_total":           all_total,
        "washer_utilization":  round(washers_in_use / washer_total * 100, 1) if washer_total > 0 else 0,
        "dryer_utilization":  round(dryers_in_use  / dryer_total  * 100, 1) if dryer_total  > 0 else 0,
        "overall_utilization": round(all_in_use     / all_total    * 100, 1) if all_total    > 0 else 0,
    }

    # Append to private CSV log (migrate to add day_of_week and utilization if needed)
    os.makedirs("data", exist_ok=True)
    file_exists = os.path.isfile(DATA_FILE)
    if file_exists:
        with open(DATA_FILE, "r") as f:
            first_line = f.readline()
        needs_migrate = "day_of_week" not in first_line or "washer_utilization" not in first_line
        if needs_migrate:
            with open(DATA_FILE, "r") as f:
                all_rows = list(csv.DictReader(f))
            for r in all_rows:
                if "day_of_week" not in r or r.get("day_of_week") == "":
                    try:
                        dt = datetime.strptime(r["timestamp"], "%Y-%m-%d %H:%M:%S")
                        r["day_of_week"] = dt.weekday()
                    except (ValueError, KeyError):
                        r["day_of_week"] = 0
                if "washer_utilization" not in r or r.get("washer_utilization") == "":
                    wtot = int(r.get("washers_total", 0))
                    wuse = int(r.get("washers_in_use", 0))
                    r["washer_utilization"] = round(wuse / wtot * 100, 1) if wtot > 0 else 0
                if "dryer_utilization" not in r or r.get("dryer_utilization") == "":
                    dtot = int(r.get("dryers_total", 0))
                    duse = int(r.get("dryers_in_use", 0))
                    r["dryer_utilization"] = round(duse / dtot * 100, 1) if dtot > 0 else 0
                if "overall_utilization" not in r or r.get("overall_utilization") == "":
                    atot = int(r.get("all_total", 0))
                    ause = int(r.get("all_in_use", 0))
                    r["overall_utilization"] = round(ause / atot * 100, 1) if atot > 0 else 0
            with open(DATA_FILE, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=row.keys(), extrasaction="ignore")
                writer.writeheader()
                for r in all_rows:
                    writer.writerow(r)
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
                wt = int(r.get("washers_total", 0))
                wuse = int(r.get("washers_in_use", 0))
                dtot = int(r.get("dryers_total", 0))
                duse = int(r.get("dryers_in_use", 0))
                atot = int(r.get("all_total", 0))
                ause = int(r.get("all_in_use", 0))
                wu = r.get("washer_utilization", "")
                du = r.get("dryer_utilization", "")
                ou = r.get("overall_utilization", "")
                wupct = float(wu) if wu != "" and str(wu).strip() else (wuse / wt * 100 if wt > 0 else 0)
                dupct = float(du) if du != "" and str(du).strip() else (duse / dtot * 100 if dtot > 0 else 0)
                oupct = float(ou) if ou != "" and str(ou).strip() else (ause / atot * 100 if atot > 0 else 0)
                history.append({
                    "timestamp":           r["timestamp"],
                    "hour_pst":            int(r["hour_pst"]),
                    "day_of_week":         int(r.get("day_of_week", 0)),
                    "washers_free":        int(r["washers_free"]),
                    "washers_in_use":      wuse,
                    "washers_total":       wt,
                    "dryers_free":         int(r["dryers_free"]),
                    "dryers_in_use":       duse,
                    "dryers_total":        dtot,
                    "all_free":            int(r["all_free"]),
                    "all_in_use":          ause,
                    "all_total":           atot,
                    "washer_utilization":  round(wupct, 1),
                    "dryer_utilization":   round(dupct, 1),
                    "overall_utilization": round(oupct, 1),
                })

    # Write sanitized public summary — safe to expose
    os.makedirs("docs", exist_ok=True)
    summary = {
        "last_updated":   timestamp,
        "current": {
            "washers_free":       row["washers_free"],
            "washers_in_use":     row["washers_in_use"],
            "washers_total":      row["washers_total"],
            "dryers_free":        row["dryers_free"],
            "dryers_in_use":      row["dryers_in_use"],
            "dryers_total":       row["dryers_total"],
            "all_free":           row["all_free"],
            "all_in_use":         row["all_in_use"],
            "all_total":          row["all_total"],
            "washer_utilization": row["washer_utilization"],
            "dryer_utilization":  row["dryer_utilization"],
            "overall_utilization": row["overall_utilization"],
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
