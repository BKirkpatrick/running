import { getCollection, render } from 'astro:content';
import { DAY, mondayOf, type Run } from './runs';

// Ridge geometry: one ridge per week, each drawn relative to its own baseline at y = 0.
export const RIDGE = { width: 480, spacing: 14, amp: 70 };

export interface Ridge {
	t: number; // Monday
	runs: Run[];
	km: number;
	line: string;
	fill: string;
	top: number; // highest point of the line (most negative y)
}

const sum = (xs: number[]) => xs.reduce((a, b) => a + b, 0);

export function ridges(runs: Run[], { samples = 120, sigma = 0.3 } = {}): Ridge[] {
	const { width, amp } = RIDGE;
	const t0 = runs[0]?.t ?? 0;
	const t1 = runs.at(-1)?.t ?? 0;
	const weeks: { t: number; runs: Run[] }[] = [];
	for (let w = mondayOf(t0); w <= t1; w += 7 * DAY) {
		weeks.push({ t: w, runs: runs.filter((r) => r.t >= w && r.t < w + 7 * DAY) });
	}

	// Each run is a Gaussian bump centred on midday of its day; sigma is in days.
	const height = (w: (typeof weeks)[number], d: number) =>
		sum(w.runs.map((r) => r.km * Math.exp(-(((d - ((r.t - w.t) / DAY + 0.5)) / sigma) ** 2) / 2)));
	const curves = weeks.map((w) => Array.from({ length: samples + 1 }, (_, i) => height(w, (i / samples) * 7)));
	const peak = Math.max(...curves.flat()) || 1;

	return weeks.map((w, k) => {
		const pts = curves[k].map((v, i) => `${(i / samples) * width},${-(v / peak) * amp}`);
		return {
			...w,
			top: -(Math.max(...curves[k]) / peak) * amp,
			km: sum(w.runs.map((r) => r.km)),
			line: `M${pts.join('L')}`,
			fill: `M0,0L${pts.join('L')}L${width},0Z`,
		};
	});
}

/** Written logs, newest first, with rendered markdown and that day's distance and pace. */
export async function loadLogs(runs: Run[]) {
	const logs = (await getCollection('logs')).sort((a, b) => b.id.localeCompare(a.id));
	return Promise.all(
		logs.map(async (log) => {
			const t = Date.parse(log.id);
			const { Content } = await render(log);
			const sameDay = runs.filter((r) => r.t === t);
			const km = sum(sameDay.map((r) => r.km));
			const seconds = sum(sameDay.map((r) => r.seconds));
			return { id: log.id, t, week: mondayOf(t), Content, km, pace: km ? seconds / 60 / km : null };
		}),
	);
}

export type Log = Awaited<ReturnType<typeof loadLogs>>[number];
