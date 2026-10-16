"""Build the website's runs table from the Nike backlog plus live Strava runs.

- data/runs_init.parquet: sanitised Nike TCX backlog (committed, never changes)
- Strava: every run on the account, fetched fresh from the API on each build

Nothing from Strava is stored between builds. Strava runs on or before the last
Nike run date are dropped so the backlog isn't double counted. The output keeps
the sanitised columns both sources share (no times, GPS, heart rate or source
IDs) plus a ``source`` column, and is gitignored because it's rebuilt each time.

Credentials come from environment variables (GitHub Actions secrets), falling
back to ``.env`` and the tokens saved by testing-strava-api.ipynb when run
locally: STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, STRAVA_REFRESH_TOKEN.

Usage:
    uv run scripts/build_runs.py [--init PATH] [--output PATH]
"""

import argparse
import hashlib
import hmac
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INIT = REPO_ROOT / "data" / "runs_init.parquet"
DEFAULT_OUTPUT = REPO_ROOT / "data" / "runs.parquet"
LOCAL_TOKENS = REPO_ROOT / "data" / "local" / "strava" / "tokens.json"

API_BASE = "https://www.strava.com/api/v3"
TOKEN_URL = "https://www.strava.com/api/v3/oauth/token"

RUN_SPORT_TYPES = {"Run", "TrailRun", "VirtualRun"}

RUN_COLUMNS = {
    "run_id": "VARCHAR",
    "run_date": "DATE",
    "name": "VARCHAR",
    "location": "VARCHAR",
    "distance_km": "DOUBLE",
    "moving_time_s": "DOUBLE",
    "avg_pace_min_per_km": "DOUBLE",
    "max_speed_m_s": "DOUBLE",
    "source": "VARCHAR",
}


def load_dotenv(path: Path = REPO_ROOT / ".env") -> None:
    """Minimal KEY=VALUE reader; real environment variables take precedence."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} is not set (GitHub secret, environment variable or .env)")
    return value


def http_json(request: urllib.request.Request) -> dict | list:
    try:
        with urllib.request.urlopen(request) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{request.get_method()} {request.full_url} -> {e.code}: {e.read().decode()}") from None


def get_access_token(client_id: str, client_secret: str) -> str:
    """Swap the refresh token for an access token.

    Strava may rotate the refresh token, which invalidates the old one. Locally
    the new one is saved; in CI it can't be, so a warning asks for the secret
    to be updated before the next build fails.
    """
    from_file = "STRAVA_REFRESH_TOKEN" not in os.environ and LOCAL_TOKENS.exists()
    if from_file:
        tokens = json.loads(LOCAL_TOKENS.read_text())
        refresh_token = tokens["refresh_token"]
    else:
        refresh_token = require_env("STRAVA_REFRESH_TOKEN")

    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }).encode()
    try:
        response = http_json(urllib.request.Request(TOKEN_URL, data=data, method="POST"))
    except SystemExit as e:
        raise SystemExit(
            f"{e}\nStrava rejected the refresh token. Re-authorise with testing-strava-api.ipynb "
            "(step 1) and update the STRAVA_REFRESH_TOKEN secret."
        ) from None

    if response["refresh_token"] != refresh_token:
        if from_file:
            tokens.update(
                access_token=response["access_token"],
                refresh_token=response["refresh_token"],
                expires_at=response["expires_at"],
            )
            LOCAL_TOKENS.write_text(json.dumps(tokens, indent=2))
        else:
            # GitHub Actions annotation; the old token is now dead, so the next build will fail.
            print(
                "::warning::Strava rotated the refresh token. Re-authorise with testing-strava-api.ipynb "
                "and update the STRAVA_REFRESH_TOKEN secret before the next build."
            )
    return response["access_token"]


def fetch_activities(access_token: str) -> list[dict]:
    activities, page = [], 1
    while True:
        query = urllib.parse.urlencode({"page": page, "per_page": 200})
        request = urllib.request.Request(
            f"{API_BASE}/athlete/activities?{query}",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        batch = http_json(request)
        if not batch:
            return activities
        activities.extend(batch)
        page += 1


def public_run_id(activity_id: int, key: str) -> str:
    """Stable, non-reversible ID so the public data doesn't link to the Strava activity."""
    digest = hmac.new(key.encode(), f"strava:{activity_id}".encode(), hashlib.sha256).digest()
    return str(uuid.UUID(bytes=digest[:16]))


def shape_run(activity: dict, id_key: str) -> dict:
    distance_km = activity["distance"] / 1000
    moving_time_s = float(activity["moving_time"])
    indoors = activity.get("trainer") or activity["sport_type"] == "VirtualRun"
    return {
        "run_id": public_run_id(activity["id"], id_key),
        # start_date_local is local wall-clock time despite its trailing Z.
        "run_date": activity["start_date_local"][:10],
        "name": activity["name"],
        "location": "indoors" if indoors else "outdoors",
        "distance_km": distance_km,
        "moving_time_s": moving_time_s,
        "avg_pace_min_per_km": (moving_time_s / 60) / distance_km,
        "max_speed_m_s": activity.get("max_speed"),
        "source": "strava",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--init", type=Path, default=DEFAULT_INIT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    load_dotenv()
    client_id = require_env("STRAVA_CLIENT_ID")
    client_secret = require_env("STRAVA_CLIENT_SECRET")

    con = duckdb.connect()
    schema = ", ".join(f"{name} {dtype}" for name, dtype in RUN_COLUMNS.items())
    con.execute(f"CREATE TABLE runs ({schema})")
    # runs_init.parquet is written by scripts/ingest_nike_tcx.py in this same schema.
    con.execute(f"INSERT INTO runs BY NAME SELECT * FROM read_parquet('{args.init}')")
    cutoff = con.execute("SELECT max(run_date) FROM runs").fetchone()[0]

    activities = fetch_activities(get_access_token(client_id, client_secret))
    runs = [
        a for a in activities
        if a["sport_type"] in RUN_SPORT_TYPES and a["distance"] > 0
    ]
    strava_runs = [shape_run(a, client_secret) for a in runs]
    new_runs = [r for r in strava_runs if cutoff is None or r["run_date"] > cutoff.isoformat()]

    placeholders = ", ".join("?" for _ in RUN_COLUMNS)
    con.executemany(
        f"INSERT INTO runs VALUES ({placeholders})",
        [[row[name] for name in RUN_COLUMNS] for row in new_runs],
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"""
        COPY (
            SELECT * REPLACE (
                round(moving_time_s)::INTEGER AS moving_time_s,
                round(distance_km, 2) AS distance_km,
                round(avg_pace_min_per_km, 2) AS avg_pace_min_per_km,
                round(max_speed_m_s, 2) AS max_speed_m_s
            )
            FROM runs ORDER BY run_date, run_id
        ) TO '{args.output}' (FORMAT parquet)
    """)

    nike_count = con.execute("SELECT count(*) FROM runs WHERE source = 'nike'").fetchone()[0]
    print(f"Strava: {len(activities)} activities, {len(runs)} runs, "
          f"{len(runs) - len(new_runs)} dropped as on or before {cutoff} (Nike backlog)")
    print(f"Wrote {nike_count + len(new_runs)} runs ({nike_count} Nike + {len(new_runs)} Strava) to {args.output}")


if __name__ == "__main__":
    main()
