import { existsSync } from 'node:fs';
import { asyncBufferFromFile, parquetReadObjects } from 'hyparquet';

export const DAY = 86_400_000;

export interface Run {
	day: string; // YYYY-MM-DD
	t: number; // UTC midnight, ms
	km: number;
	seconds: number;
}

const isoDay = (d: Date | string) => (d instanceof Date ? d.toISOString() : String(d)).slice(0, 10);

// Outdoor runs only, the few treadmill runs I did early on were crap from data collection perspective.
// data/runs.parquet is built by scripts/build_runs.py; falls back to the committed Nike backlog.
export async function loadRuns(): Promise<Run[]> {
	const built = '../data/runs.parquet';
	const path = existsSync(built) ? built : '../data/runs_init.parquet';
	const rows = await parquetReadObjects({
		file: await asyncBufferFromFile(path),
		columns: ['run_date', 'distance_km', 'moving_time_s', 'location'],
	});
	return rows
		.filter((row) => row.location === 'outdoors')
		.map((row) => {
			const day = isoDay(row.run_date);
			return { day, t: Date.parse(day), km: Number(row.distance_km), seconds: Number(row.moving_time_s) };
		})
		.sort((a, b) => a.t - b.t);
}

/** UTC midnight on the Monday of t's week. */
export const mondayOf = (t: number) => t - ((new Date(t).getUTCDay() + 6) % 7) * DAY;

const fmt = (opts: Intl.DateTimeFormatOptions) => (t: number) =>
	new Date(t).toLocaleDateString('en-GB', { ...opts, timeZone: 'UTC' });

export const fmtDay = fmt({ day: 'numeric', month: 'short' });
export const fmtLong = fmt({ day: 'numeric', month: 'short', year: 'numeric' });

/** 6.5 -> "6:30" */
export function fmtPace(min: number): string {
	const s = Math.round(min * 60);
	return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

export const km = (v: number, dp = 1) => `${v.toFixed(dp)} km`;
