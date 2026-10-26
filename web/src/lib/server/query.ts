// Library queries: URL filters → facets + ranked hits.

import type { Facet, FacetValue, PageHit, WorkHit, WorkSummary } from '#lib/types.ts';
import { all } from './db.ts';
import { lineage, type Library } from './library.ts';
import { similarity } from './similar.ts';

export type Sort = 'auto' | 'relevance' | 'similar' | 'added' | 'year' | 'title' | 'cited' | 'random';

export interface Filters {
	q: string;
	like: number[];
	sort: Sort;
	seed: number;
	yearFrom: number | null;
	yearTo: number | null;
	limit: number;
	facets: Record<string, string[]>;
}

/** Facet keys, in display order. */
export const FACETS: { key: string; label: string; limit: number }[] = [
	{ key: 'kind', label: 'Kind', limit: 20 },
	{ key: 'arxiv', label: 'arXiv category', limit: 12 },
	{ key: 'subject', label: 'Subject', limit: 12 },
	{ key: 'field', label: 'Field (model)', limit: 12 },
	{ key: 'author', label: 'Author', limit: 10 },
	{ key: 'folder', label: 'Folder', limit: 8 },
	{ key: 'origin', label: 'Downloaded from', limit: 8 },
	{ key: 'status', label: 'Status', limit: 8 }
];

export function parseFilters(url: URL): Filters {
	const sp = url.searchParams;
	const int = (k: string) => {
		const v = sp.get(k);
		const n = v ? Number.parseInt(v, 10) : NaN;
		return Number.isFinite(n) ? n : null;
	};
	const facets: Record<string, string[]> = {};
	for (const f of FACETS) {
		const vs = sp.getAll(f.key).filter(Boolean);
		if (vs.length) facets[f.key] = vs;
	}
	const like = (sp.get('like') ?? '')
		.split(',')
		.map((s) => Number.parseInt(s, 10))
		.filter((n) => Number.isFinite(n));
	return {
		q: (sp.get('q') ?? '').trim(),
		like,
		sort: (sp.get('sort') as Sort) || 'auto',
		seed: int('seed') ?? 1,
		yearFrom: int('from'),
		yearTo: int('to'),
		limit: Math.min(int('limit') ?? 60, 2000),
		facets
	};
}

// ---------------------------------------------------------------------------
// Facet values of a work

function facetValues(lib: Library, w: WorkSummary, key: string): { value: string; label: string; hint?: string }[] {
	switch (key) {
		case 'kind':
			return [{ value: w.kind ?? 'unknown', label: w.kind ?? 'unknown' }];
		case 'status':
			return [{ value: w.status, label: w.status }];
		case 'arxiv':
			return w.topics
				.filter((t) => t.scheme === 'arxiv' && t.level === 'topic')
				.map((t) => ({ value: t.name, label: t.name }));
		case 'subject': {
			// OpenAlex subfields (the level between field and topic).
			const seen = new Map<string, { value: string; label: string; hint?: string }>();
			for (const t of w.topics) {
				if (t.scheme !== 'openalex') continue;
				for (const a of lineage(lib, t.id))
					if (a.level === 'subfield') {
						const field = a.parentId != null ? lib.topics.get(a.parentId)?.name : undefined;
						seen.set(String(a.id), { value: String(a.id), label: a.name, hint: field });
					}
			}
			return [...seen.values()];
		}
		case 'field': {
			const seen = new Map<string, { value: string; label: string }>();
			for (const t of w.topics) {
				if (t.scheme !== 'llm') continue;
				const f = t.level === 'field' ? lib.topics.get(t.id) : t.parentId != null ? lib.topics.get(t.parentId) : undefined;
				if (f) seen.set(String(f.id), { value: String(f.id), label: f.name });
			}
			return [...seen.values()];
		}
		case 'author':
			return w.authors.map((a) => ({
				value: a.personId != null ? `p${a.personId}` : `n:${a.name}`,
				label: a.name
			}));
		case 'folder':
			return w.folders.map((f) => ({ value: f, label: f }));
		case 'origin':
			return w.origins.map((o) => ({ value: o, label: o }));
	}
	return [];
}

// ---------------------------------------------------------------------------
// Full-text search

interface SearchMatch {
	score: number;
	titleHl?: string;
	abstractHl?: string;
	pageHits: PageHit[];
}

const HL_OPEN = '\u0002';
const HL_CLOSE = '\u0003';

/** Turn free text into a safe FTS5 query: every word must match, last one as a prefix. */
export function ftsQuery(q: string): string {
	const tokens = q.match(/[\p{L}\p{N}]+/gu) ?? [];
	return tokens.map((t, i) => `"${t}"${i === tokens.length - 1 ? '*' : ''}`).join(' ');
}

/** Queries that look like deliberate FTS5 syntax are tried verbatim first. */
function looksAdvanced(q: string): boolean {
	return /["*:()^]|\b(AND|OR|NOT|NEAR)\b/.test(q);
}

function runFts<T>(sql: string, q: string): T[] {
	const attempts = looksAdvanced(q) ? [q, ftsQuery(q)] : [ftsQuery(q)];
	for (const a of attempts) {
		if (!a) continue;
		try {
			return all<T>(sql, a);
		} catch {
			/* FTS5 syntax error: fall back to the tokenised form */
		}
	}
	return [];
}

export function search(lib: Library, q: string): Map<number, SearchMatch> {
	const out = new Map<number, SearchMatch>();
	const meta = runFts<{ id: number; rank: number; title_hl: string; abs: string }>(
		`SELECT rowid AS id, bm25(works_fts, 10.0, 4.0, 2.0, 3.0) AS rank,
		        highlight(works_fts, 0, '${HL_OPEN}', '${HL_CLOSE}') AS title_hl,
		        snippet(works_fts, 2, '${HL_OPEN}', '${HL_CLOSE}', '…', 48) AS abs
		 FROM works_fts WHERE works_fts MATCH ? ORDER BY rank LIMIT 1000`,
		q
	);
	for (const r of meta) {
		out.set(r.id, {
			score: -r.rank,
			titleHl: r.title_hl?.includes(HL_OPEN) ? r.title_hl : undefined,
			abstractHl: r.abs?.includes(HL_OPEN) ? r.abs : undefined,
			pageHits: []
		});
	}
	const pages = runFts<{ file_id: number; page: number; snip: string; rank: number }>(
		`SELECT p.file_id, p.page, snippet(file_pages_fts, 0, '${HL_OPEN}', '${HL_CLOSE}', '…', 20) AS snip,
		        bm25(file_pages_fts) AS rank
		 FROM file_pages_fts JOIN file_pages p ON p.id = file_pages_fts.rowid
		 WHERE file_pages_fts MATCH ? ORDER BY rank LIMIT 2000`,
		q
	);
	for (const r of pages) {
		for (const wid of lib.fileToWorks.get(r.file_id) ?? []) {
			let m = out.get(wid);
			if (!m) {
				m = { score: 0, pageHits: [] };
				out.set(wid, m);
			}
			if (m.pageHits.length === 0) m.score += -r.rank * 0.5;
			else m.score += -r.rank * 0.05;
			if (m.pageHits.length < 4) m.pageHits.push({ fileId: r.file_id, page: r.page, snippet: r.snip });
		}
	}
	return out;
}

// ---------------------------------------------------------------------------

function mulberry32(seed: number) {
	return () => {
		seed |= 0;
		seed = (seed + 0x6d2b79f5) | 0;
		let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
		t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
		return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
	};
}

export interface QueryResult {
	hits: WorkHit[];
	total: number;
	facets: Facet[];
	years: { year: number; count: number }[];
	yearRange: { min: number; max: number } | null;
	sort: Exclude<Sort, 'auto'>;
	seeds: { id: number; title: string | null }[];
}

export function runQuery(lib: Library, f: Filters): QueryResult {
	const matches = f.q ? search(lib, f.q) : null;
	const sims = f.like.length ? similarity(lib.tfidf, f.like) : null;

	// Rejected works (personal documents) are hidden unless asked for.
	const facetSel = { ...f.facets };
	if (!facetSel.status) facetSel.status = ['candidate', 'confirmed'];

	const keys = Object.keys(facetSel);
	const valuesOf = new Map<number, Record<string, { value: string; label: string; hint?: string }[]>>();
	for (const w of lib.works) {
		const rec: Record<string, { value: string; label: string; hint?: string }[]> = {};
		for (const fd of FACETS) rec[fd.key] = facetValues(lib, w, fd.key);
		valuesOf.set(w.id, rec);
	}

	const base = lib.works.filter((w) => !matches || matches.has(w.id));
	const passYear = (w: WorkSummary) =>
		(f.yearFrom == null || (w.year != null && w.year >= f.yearFrom)) &&
		(f.yearTo == null || (w.year != null && w.year <= f.yearTo));
	const passFacet = (w: WorkSummary, key: string) => {
		const sel = facetSel[key];
		if (!sel?.length) return true;
		return valuesOf.get(w.id)![key].some((v) => sel.includes(v.value));
	};
	const passAll = (w: WorkSummary, except?: string) =>
		keys.every((k) => k === except || passFacet(w, k)) && (except === 'year' || passYear(w));

	// Facet values and their order come from a fixed universe (every work that
	// passes the status selection), not from the current result set, so rows
	// never move or disappear under the cursor as filters are toggled; only the
	// counts change. Status itself is ranked over all works.
	const statusSel = facetSel.status;
	const inUniverse = (w: WorkSummary, key: string) => key === 'status' || statusSel.includes(w.status);

	const facets: Facet[] = FACETS.map((fd) => {
		const entries = new Map<string, FacetValue & { total: number }>();
		for (const w of lib.works) {
			if (!inUniverse(w, fd.key)) continue;
			for (const v of valuesOf.get(w.id)![fd.key]) {
				const e = entries.get(v.value);
				if (e) e.total++;
				else entries.set(v.value, { ...v, count: 0, total: 1, selected: false });
			}
		}
		for (const w of base) {
			if (!passAll(w, fd.key)) continue;
			for (const v of valuesOf.get(w.id)![fd.key]) {
				const e = entries.get(v.value);
				if (e) e.count++;
			}
		}
		// Only the user's own choices count as selected (not the implicit
		// "hide rejected" status default).
		const sel = f.facets[fd.key] ?? [];
		for (const s of sel)
			if (!entries.has(s))
				entries.set(s, { value: s, label: labelFor(lib, fd.key, s), count: 0, total: 0, selected: true });
		for (const e of entries.values()) e.selected = sel.includes(e.value);
		const values = [...entries.values()]
			.sort((a, b) => b.total - a.total || a.label.localeCompare(b.label))
			.map(({ total: _total, ...v }) => v);
		return { key: fd.key, label: fd.label, values };
	});

	// Year bars: counts follow the other filters, but the axis spans the universe.
	const yearCounts = new Map<number, number>();
	for (const w of base) if (w.year != null && passAll(w, 'year')) yearCounts.set(w.year, (yearCounts.get(w.year) ?? 0) + 1);
	const years = [...yearCounts].map(([year, count]) => ({ year, count })).sort((a, b) => a.year - b.year);
	const universeYears = lib.works
		.filter((w) => inUniverse(w, 'year') && w.year != null)
		.map((w) => w.year!);
	const yearRange = universeYears.length
		? { min: Math.min(...universeYears), max: Math.max(...universeYears) }
		: null;

	let rows = base.filter((w) => passAll(w));

	const sort: Exclude<Sort, 'auto'> =
		f.sort !== 'auto' ? f.sort : sims ? 'similar' : matches ? 'relevance' : 'added';
	const by = <T>(key: (w: WorkSummary) => T | null, desc = true) => (a: WorkSummary, b: WorkSummary) => {
		const x = key(a);
		const y = key(b);
		if (x == null && y == null) return 0;
		if (x == null) return 1;
		if (y == null) return -1;
		return (x < y ? -1 : x > y ? 1 : 0) * (desc ? -1 : 1);
	};
	switch (sort) {
		case 'relevance':
			rows.sort(by((w) => matches?.get(w.id)?.score ?? 0));
			break;
		case 'similar':
			rows.sort(by((w) => sims?.get(w.id) ?? 0));
			break;
		case 'added':
			rows.sort(by((w) => w.addedAt));
			break;
		case 'year':
			rows.sort(by((w) => w.year));
			break;
		case 'title':
			rows.sort(by((w) => (w.title ?? '').toLowerCase(), false));
			break;
		case 'cited':
			rows.sort(by((w) => w.citations));
			break;
		case 'random': {
			const rnd = mulberry32(f.seed);
			const keyed = rows.map((w) => [rnd(), w] as const).sort((a, b) => a[0] - b[0]);
			rows = keyed.map(([, w]) => w);
			break;
		}
	}

	const total = rows.length;
	const hits: WorkHit[] = rows.slice(0, f.limit).map((w) => {
		const m = matches?.get(w.id);
		return {
			...w,
			score: sort === 'similar' ? sims?.get(w.id) : m?.score,
			titleHl: m?.titleHl,
			abstractHl: m?.abstractHl,
			pageHits: m?.pageHits
		};
	});

	return {
		hits,
		total,
		facets,
		years,
		yearRange,
		sort,
		seeds: f.like.map((id) => ({ id, title: lib.byId.get(id)?.title ?? null }))
	};
}

function labelFor(lib: Library, key: string, value: string): string {
	if (key === 'subject' || key === 'field') return lib.topics.get(Number(value))?.name ?? value;
	if (key === 'author') {
		if (value.startsWith('n:')) return value.slice(2);
		const pid = Number(value.slice(1));
		for (const w of lib.works) for (const a of w.authors) if (a.personId === pid) return a.name;
	}
	return value;
}
