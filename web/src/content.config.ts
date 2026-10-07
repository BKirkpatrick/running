import { defineCollection } from 'astro:content';
import { glob } from 'astro/loaders';

// Logs live at the repo root as logs/log_YYYYMMDD.md; the id becomes YYYY-MM-DD.
const logs = defineCollection({
	loader: glob({
		base: '../logs',
		pattern: 'log_*.md',
		generateId: ({ entry }) => entry.replace(/^log_(\d{4})(\d{2})(\d{2})\.md$/, '$1-$2-$3'),
	}),
});

export const collections = { logs };
