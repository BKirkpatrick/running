"""Ingest Nike Run Club TCX exports into runs and trackpoints Parquet tables.

- runs: one row per run (summary, Nike tags and derived totals)
- trackpoints: one row per recorded sample, keyed by ``run_id``
- runs_init: sanitised runs in the same columns and types as the website's
  ``data/runs.parquet`` (built by scripts/build_runs.py), committed to the repo

runs and trackpoints are unsanitised (they keep GPS, heart rate, Nike IDs and
exact times), so they are written to the gitignored ``data/local/`` directory.

Usage:
    uv run scripts/ingest_nike_tcx.py [--input DIR] [--output-dir DIR] [--init-output PATH]
"""

import argparse
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = REPO_ROOT / "data" / "raw" / "nike" / "tcx"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "data" / "local"
DEFAULT_INIT_OUTPUT = REPO_ROOT / "data" / "runs_init.parquet"

# The export carries no timezone; all runs were recorded in the GB marketplace.
LOCAL_TZ = ZoneInfo("Europe/London")

RUN_COLUMNS = {
    "run_id": "VARCHAR",
    "nike_activity_id": "VARCHAR",
    "start_time_utc": "TIMESTAMPTZ",
    "start_time_local": "TIMESTAMP",
    "run_date": "DATE",
    "name": "VARCHAR",
    "guided_run_id": "VARCHAR",
    "workout_title": "VARCHAR",
    "plan_stage": "VARCHAR",
    "goal_type": "VARCHAR",
    "location": "VARCHAR",
    "weather": "VARCHAR",
    "temperature_c": "DOUBLE",
    "distance_km": "DOUBLE",
    "moving_time_s": "DOUBLE",
    "elapsed_time_s": "DOUBLE",
    "avg_pace_min_per_km": "DOUBLE",
    "max_speed_m_s": "DOUBLE",
    "calories": "INTEGER",
    "avg_cadence_spm": "INTEGER",
    "avg_hr_bpm": "INTEGER",
    "max_hr_bpm": "INTEGER",
    "total_ascent_m": "DOUBLE",
    "total_descent_m": "DOUBLE",
    "has_gps": "BOOLEAN",
    "has_hr": "BOOLEAN",
    "auto_pause_enabled": "BOOLEAN",
    "trackpoint_count": "INTEGER",
    "recording_app_version": "VARCHAR",
    "source_file": "VARCHAR",
}

# Trackpoints are sparse: each sample carries only some measurements, so most
# columns are NULL on any given row. Nike's DistanceMeters is the distance
# since the previous sample (not cumulative as the TCX spec says), so both the
# delta and a running total are kept.
TRACKPOINT_COLUMNS = {
    "run_id": "VARCHAR",
    "seq": "INTEGER",
    "time_utc": "TIMESTAMPTZ",
    "elapsed_s": "DOUBLE",
    "gap_s": "DOUBLE",
    "distance_delta_m": "DOUBLE",
    "distance_m": "DOUBLE",
    "speed_m_s": "DOUBLE",
    "pace_min_per_km": "DOUBLE",
    "hr_bpm": "INTEGER",
    "latitude": "DOUBLE",
    "longitude": "DOUBLE",
    "altitude_m": "DOUBLE",
    "ascent_m": "DOUBLE",
    "descent_m": "DOUBLE",
    "nike_fuel": "DOUBLE",
}


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def text(el: ET.Element | None) -> str | None:
    if el is None or el.text is None:
        return None
    return el.text.strip() or None


def child(el: ET.Element, name: str) -> ET.Element | None:
    return next((c for c in el if local_name(c.tag) == name), None)


def descendants(el: ET.Element, name: str) -> list[ET.Element]:
    return [e for e in el.iter() if local_name(e.tag) == name]


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def to_float(value: str | None) -> float | None:
    return float(value) if value is not None else None


def to_int(value: str | None) -> int | None:
    return round(float(value)) if value is not None else None


def parse_tags(activity: ET.Element) -> dict[str, str]:
    """Nike stores metadata as alternating <Key>/<Value> siblings inside <Tag>."""
    tags = {}
    for tag in descendants(activity, "Tag"):
        items = list(tag)
        for key, value in zip(items[::2], items[1::2]):
            tags[text(key)] = text(value)
    return tags


def first_text(el: ET.Element, name: str) -> str | None:
    """Text of the first descendant called ``name``, if any."""
    return next((text(e) for e in el.iter() if local_name(e.tag) == name), None)


def parse_trackpoints(run_id: str, start: datetime, trackpoints: list[ET.Element]) -> list[dict]:
    rows = []
    distance_m = 0.0
    previous = start
    for seq, tp in enumerate(trackpoints):
        time = parse_time(text(child(tp, "Time")))
        delta = to_float(text(child(tp, "DistanceMeters")))
        distance_m += delta or 0.0
        position = child(tp, "Position")
        hr = child(tp, "HeartRateBpm")
        rows.append({
            "run_id": run_id,
            "seq": seq,
            "time_utc": time,
            "elapsed_s": (time - start).total_seconds(),
            "gap_s": (time - previous).total_seconds(),
            "distance_delta_m": delta,
            "distance_m": distance_m,
            "speed_m_s": to_float(first_text(tp, "Speed")),
            "pace_min_per_km": to_float(first_text(tp, "Pace")),
            "hr_bpm": to_int(text(child(hr, "Value"))) if hr is not None else None,
            "latitude": to_float(text(child(position, "LatitudeDegrees"))) if position is not None else None,
            "longitude": to_float(text(child(position, "LongitudeDegrees"))) if position is not None else None,
            "altitude_m": to_float(text(child(tp, "AltitudeMeters"))),
            "ascent_m": to_float(first_text(tp, "AscentInMeters")),
            "descent_m": to_float(first_text(tp, "DescentInMeters")),
            "nike_fuel": to_float(first_text(tp, "NikeFuel")),
        })
        previous = time
    return rows


def parse_run(path: Path) -> tuple[dict, list[dict]]:
    root = ET.parse(path).getroot()
    activity = descendants(root, "Activity")[0]
    laps = descendants(activity, "Lap")
    if len(laps) != 1:
        raise ValueError(f"{path.name}: expected 1 lap, found {len(laps)}")
    lap = laps[0]
    tags = parse_tags(activity)
    trackpoints = descendants(lap, "Trackpoint")

    start = parse_time(text(child(activity, "Id")))
    end = parse_time(text(child(trackpoints[-1], "Time")))
    distance_m = float(text(child(lap, "DistanceMeters")))
    moving_time_s = float(text(child(lap, "TotalTimeSeconds")))
    avg_hr = child(lap, "AverageHeartRateBpm")
    max_hr = child(lap, "MaximumHeartRateBpm")

    run = {
        "run_id": path.stem,
        "nike_activity_id": tags.get("com.nike.running.originalactivityid"),
        "start_time_utc": start,
        "start_time_local": start.astimezone(LOCAL_TZ).replace(tzinfo=None),
        "run_date": start.astimezone(LOCAL_TZ).date(),
        "name": tags.get("com.nike.name"),
        "guided_run_id": tags.get("com.nike.guidedactivity.id"),
        "workout_title": tags.get("com.nike.nrc.program.workout.title"),
        "plan_stage": tags.get("com.nike.nrc.program.active.stage.subtitle"),
        "goal_type": tags.get("com.nike.running.goaltype"),
        "location": tags.get("location"),
        "weather": tags.get("com.nike.weather"),
        "temperature_c": to_float(tags.get("com.nike.temperature")),
        "distance_km": distance_m / 1000,
        "moving_time_s": moving_time_s,
        "elapsed_time_s": (end - start).total_seconds(),
        "avg_pace_min_per_km": (moving_time_s / 60) / (distance_m / 1000),
        "max_speed_m_s": to_float(text(child(lap, "MaximumSpeed"))),
        "calories": to_int(text(child(lap, "Calories"))),
        "avg_cadence_spm": to_int(text(child(lap, "Cadence"))),
        "avg_hr_bpm": to_int(text(child(avg_hr, "Value"))) if avg_hr is not None else None,
        "max_hr_bpm": to_int(text(child(max_hr, "Value"))) if max_hr is not None else None,
        "total_ascent_m": sum(float(text(e)) for e in descendants(lap, "AscentInMeters")),
        "total_descent_m": sum(float(text(e)) for e in descendants(lap, "DescentInMeters")),
        "has_gps": bool(descendants(lap, "Position")),
        "has_hr": bool(descendants(lap, "HeartRateBpm")),
        # Nike tags every trackpoint with an auto_pause Halt when the setting is on.
        "auto_pause_enabled": bool(descendants(lap, "Halt")),
        "trackpoint_count": len(trackpoints),
        "recording_app_version": tags.get("com.nike.running.recordingappversion"),
        "source_file": path.name,
    }
    return run, parse_trackpoints(path.stem, start, trackpoints)


# Public schema shared with scripts/build_runs.py: no times, GPS, heart rate or
# source IDs. Numbers are rounded; moving time is whole seconds.
INIT_SELECT = """
    SELECT
        run_id::VARCHAR AS run_id,
        run_date::DATE AS run_date,
        name::VARCHAR AS name,
        location::VARCHAR AS location,
        round(distance_km, 2)::DOUBLE AS distance_km,
        round(moving_time_s)::INTEGER AS moving_time_s,
        round(avg_pace_min_per_km, 2)::DOUBLE AS avg_pace_min_per_km,
        round(max_speed_m_s, 2)::DOUBLE AS max_speed_m_s,
        'nike'::VARCHAR AS source
    FROM runs
    ORDER BY run_date, run_id
"""


def write_parquet(
    con: duckdb.DuckDBPyConnection,
    table: str,
    columns: dict[str, str],
    rows: list[dict],
    order_by: str,
    output: Path,
) -> None:
    schema = ", ".join(f"{name} {dtype}" for name, dtype in columns.items())
    con.execute(f"CREATE TABLE {table} ({schema})")
    placeholders = ", ".join("?" for _ in columns)
    con.executemany(
        f"INSERT INTO {table} VALUES ({placeholders})",
        [[row[name] for name in columns] for row in rows],
    )
    con.execute(f"COPY (SELECT * FROM {table} ORDER BY {order_by}) TO '{output}' (FORMAT parquet)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--init-output", type=Path, default=DEFAULT_INIT_OUTPUT)
    args = parser.parse_args()

    files = sorted(args.input.glob("*.tcx"))
    if not files:
        raise SystemExit(f"No .tcx files found in {args.input}")

    runs, trackpoints = [], []
    for path in files:
        run, points = parse_run(path)
        runs.append(run)
        trackpoints.extend(points)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    runs_path = args.output_dir / "nike_runs.parquet"
    trackpoints_path = args.output_dir / "nike_trackpoints.parquet"

    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    write_parquet(con, "runs", RUN_COLUMNS, runs, "start_time_utc", runs_path)
    write_parquet(con, "trackpoints", TRACKPOINT_COLUMNS, trackpoints, "run_id, seq", trackpoints_path)
    con.execute(f"COPY ({INIT_SELECT}) TO '{args.init_output}' (FORMAT parquet)")
    print(f"Wrote {len(runs)} runs to {runs_path}")
    print(f"Wrote {len(runs)} sanitised runs to {args.init_output}")
    print(f"Wrote {len(trackpoints)} trackpoints to {trackpoints_path}")


if __name__ == "__main__":
    main()
