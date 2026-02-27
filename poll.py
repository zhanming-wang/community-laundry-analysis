import requests
import csv
import json
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

LAX = ZoneInfo("America/Los_Angeles")
LOCATION_ID = os.environ["LOCATION_ID"]
ROOM_ID     = os.environ["ROOM_ID"]
DATA_FILE     = "data/laundry_log.csv"
SUMMARY_FILE  = "docs/summary.json"
MACHINES_FILE = "data/machines_log.csv"
MANIFEST_FILE = "data/machine_manifest.json"
MANIFEST_MIN_POLLS = 50


def load_manifest():
    """Return dict of opaque_id -> {sticker, machine_type, controller_type} or empty dict."""
    if not os.path.isfile(MANIFEST_FILE):
        return {}
    with open(MANIFEST_FILE) as f:
        return json.load(f)


def update_manifest(machines):
    """
    Auto-derive the expected machine set from first MANIFEST_MIN_POLLS appearances.
    A machine is added to the manifest once it has appeared in >= 80% of polls
    after the minimum poll threshold is reached.
    Never removes machines automatically — removals must be manual.
    """
    manifest = load_manifest()

    counts_file = "data/machine_appearance_counts.json"
    counts = {}
    if os.path.isfile(counts_file):
        with open(counts_file) as f:
            counts = json.load(f)

    for m in machines:
        oid = m.get("opaqueId", "")
        if not oid:
            continue
        if oid not in counts:
            counts[oid] = {"seen": 0, "total_polls": 0}
        counts[oid]["seen"] += 1

    total_polls = max((v["total_polls"] for v in counts.values()), default=0) + 1
    for oid in counts:
        counts[oid]["total_polls"] = total_polls

    if total_polls >= MANIFEST_MIN_POLLS:
        for m in machines:
            oid = m.get("opaqueId", "")
            if not oid or oid in manifest:
                continue
            appearance_rate = counts[oid]["seen"] / total_polls
            if appearance_rate >= 0.80:
                manifest[oid] = {
                    "sticker":         m.get("stickerNumber", ""),
                    "machine_type":    m.get("type", ""),
                    "controller_type": m.get("controllerType", ""),
                }

    os.makedirs("data", exist_ok=True)
    with open(MANIFEST_FILE, "w") as f:
        json.dump(manifest, f, indent=2)
    with open(counts_file, "w") as f:
        json.dump(counts, f, indent=2)

    return manifest


def log_machines(machines, timestamp, hour_pst, minute, day_of_week):
    """Log every API field per machine for ML training."""
    if not machines:
        return
    rows = []
    for m in machines:
        s = m.get("settings") or {}
        c = m.get("capability") or {}
        rows.append({
            "timestamp":             timestamp,
            "hour_pst":              hour_pst,
            "minute":                minute,
            "day_of_week":           day_of_week,
            "poll_type":             "scheduled",
            "opaque_id":             m.get("opaqueId", ""),
            "sticker":               m.get("stickerNumber", ""),
            "license_plate":         m.get("licensePlate", ""),
            "nfc_id":                m.get("nfcId", ""),
            "qr_code_id":            m.get("qrCodeId", ""),
            "machine_type":          m.get("type", ""),
            "controller_type":       m.get("controllerType", ""),
            "available":             int(bool(m.get("available", True))),
            "mode":                  m.get("mode", ""),
            "time_remaining":        m.get("timeRemaining", 0),
            "door_closed":           int(bool(m.get("doorClosed", True))),
            "in_service":            int(m.get("inService") is not None),
            "not_available_reason":  m.get("notAvailableReason") or "",
            "free_play":             int(bool(m.get("freePlay", False))),
            "display":               m.get("display") or "",
            "group_id":              m.get("groupId") or "",
            "soil":                  s.get("soil") or "",
            "cycle":                 s.get("cycle") or "",
            "washer_temp":           s.get("washerTemp") or "",
            "dryer_temp":            s.get("dryerTemp") or "",
            "can_add_time":          int(bool(c.get("addTime", False))),
            "show_settings":         int(bool(c.get("showSettings", False))),
            "was_missing":           0,
        })
    os.makedirs("data", exist_ok=True)
    file_exists = os.path.isfile(MACHINES_FILE)
    with open(MACHINES_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows)


def log_missing_machines(manifest, returned_ids, timestamp, hour_pst, minute, day_of_week):
    """
    For every machine in the manifest that did NOT appear in this poll's API response,
    write a synthetic row with was_missing=1. This is the primary supervised label source.
    """
    missing_ids = set(manifest.keys()) - returned_ids
    if not missing_ids:
        return

    rows = []
    for oid in missing_ids:
        info = manifest[oid]
        rows.append({
            "timestamp":             timestamp,
            "hour_pst":              hour_pst,
            "minute":                minute,
            "day_of_week":           day_of_week,
            "poll_type":             "scheduled",
            "opaque_id":             oid,
            "sticker":               info.get("sticker", ""),
            "license_plate":         "",
            "nfc_id":                "",
            "qr_code_id":            "",
            "machine_type":          info.get("machine_type", ""),
            "controller_type":       info.get("controller_type", ""),
            "available":             0,
            "mode":                  "offline",
            "time_remaining":        0,
            "door_closed":           0,
            "in_service":            0,
            "not_available_reason":  "missing_from_api",
            "free_play":             0,
            "display":               "",
            "group_id":              "",
            "soil":                  "",
            "cycle":                 "",
            "washer_temp":           "",
            "dryer_temp":            "",
            "can_add_time":          0,
            "show_settings":         0,
            "was_missing":           1,
        })

    file_exists = os.path.isfile(MACHINES_FILE)
    with open(MACHINES_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows)


def get_machines():
    url = f"https://mycscgo.com/api/v3/location/{LOCATION_ID}/room/{ROOM_ID}/machines"
    return requests.get(url, timeout=10).json()


def main():
    now_lax   = datetime.now(LAX)
    timestamp = now_lax.strftime("%Y-%m-%d %H:%M:%S")
    hour_pst  = now_lax.hour
    minute    = now_lax.minute
    day_of_week = now_lax.weekday()  # 0=Mon, 6=Sun

    machines = get_machines()
    returned_ids = {m.get("opaqueId", "") for m in machines}
    manifest = update_manifest(machines)
    log_machines(machines, timestamp, hour_pst, minute, day_of_week)
    log_missing_machines(manifest, returned_ids, timestamp, hour_pst, minute, day_of_week)

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
        "minute":              minute,
        "day_of_week":         day_of_week,
        "poll_type":           "scheduled",
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
        needs_migrate = "day_of_week" not in first_line or "washer_utilization" not in first_line or "minute" not in first_line or "poll_type" not in first_line
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
                if "minute" not in r or r.get("minute") == "":
                    try:
                        dt = datetime.strptime(r["timestamp"], "%Y-%m-%d %H:%M:%S")
                        r["minute"] = dt.minute
                    except (ValueError, KeyError):
                        r["minute"] = 0
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
                if "poll_type" not in r or r.get("poll_type") == "":
                    r["poll_type"] = "scheduled"
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

    # Read last 336 rows (7 days × 48 polls/day) for history — NO location IDs written here
    history = []
    if os.path.isfile(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            reader = list(csv.DictReader(f))
            for r in reader[-336:]:
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
                    "minute":              int(r.get("minute", 0)),
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
            "minute":             row["minute"],
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

    print(f"[{timestamp} LAX | {hour_pst}:00] "
          f"Washers: {row['washers_in_use']}/{row['washers_total']} in use | "
          f"Dryers: {row['dryers_in_use']}/{row['dryers_total']} in use | "
          f"Manifest: {len(manifest)} machines")

if __name__ == "__main__":
    main()
