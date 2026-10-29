// Library queries: URL filters → facets + ranked hits.

import type { Facet, FacetValue, PageHit, WorkHit, WorkSummary } from '#lib/types.ts';
import { matchesText } from '#lib/util.ts';
import { all } from './db.ts';
import { lineage, type Library } from './library.ts';
import { similarity } from './similar.ts';

export type Sort = 'auto' | 'relevance' | 'similar' | 'added' | 'year' | 'title' | 'cited' | 'random';

export interface Filters {
	q: string;
	/** Results-only sub-filter: exact substring of title / author / abstract. Does not affect facets. */
	match: string;
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
	{ key: 'found', label: 'Found', limit: 4 }, // when it was first found (FOUND_WINDOWS)
	{ key: 'arxiv', label: 'arXiv category', limit: 12 },
	{ key: 'subject', label: 'Subject', limit: 12 },
	{ key: 'field', label: 'Field', limit: 12 }, // assigned by the local model
	{ key: 'author', label: 'Author', limit: 10 },
	{ key: 'folder', label: 'Folder', limit: 8 },
	{ key: 'origin', label: 'Site', limit: 8 }, // the host it was downloaded from
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
		match: sp.get('match') ?? '',
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

/**
 * The Found facet: windows back from now over a work's earliest discovery (addedAt).
 * They overlap (a work found ten minutes ago is in all of them), so the facet is
 * single-choice, and it always lists every window, in this order. "today" is since
 * local midnight on the server.
 */
const FOUND_WINDOWS: { value: string; label: string; hint: string; since: (now: Date) => number }[] = [
	{
		value: 'today',
		label: 'today',
		hint: 'since midnight',
		since: (now) => new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
	},
	{ value: 'hour', label: '< hour', hint: 'in the last hour', since: (now) => now.getTime() - 3600e3 },
	{ value: 'week', label: '< week', hint: 'in the last 7 days', since: (now) => now.getTime() - 7 * 86400e3 },
	{ value: 'day', label: '< day', hint: 'in the last 24 hours', since: (now) => now.getTime() - 86400e3 },
	{ value: 'month', label: '< month', hint: 'in the last 30 days', since: (now) => now.getTime() - 30 * 86400e3 }
];

function facetValues(
	lib: Library,
	w: WorkSummary,
	key: string,
	now: Date
): { value: string; label: string; hint?: string }[] {
	switch (key) {
		case 'found': {
			const t = w.addedAt ? Date.parse(w.addedAt) : NaN;
			if (!Number.isFinite(t)) return [];
			return FOUND_WINDOWS.filter((fw) => t >= fw.since(now)).map(({ value, label, hint }) => ({ value, label, hint }));
		}
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

// The ranking mirrors src/papyrus/search.py (keep the two in step):
// * pages: bm25 times a weight for the page's kind (references, index and contents pages match
//   almost anything); a file scores its best three pages with weights 1, 1/2, 1/4;
// * works: Reciprocal Rank Fusion of the metadata ranking and the page ranking, since bm25
//   values from two indexes are not on one scale.
// bm25() can't be used in an aggregate query, even inside a subquery (SQLite flattens it), hence
// the MATERIALIZED CTE. Snippets are made only for the hits shown (fillSnippets), and their rowid
// filter is written `+rowid IN (...)`: plain `rowid IN` makes FTS5 rerun the MATCH once per rowid
// (1.7 s instead of 24 ms for 180 snippets of "t"*).

interface SearchMatch {
	score: number;
	titleHl?: string;
	abstractHl?: string;
	pageHits: PageHit[];
	/** All matching pages of the work's best file. */
	matchingPages: number;
}

const RRF_K = 60;
const KIND_WEIGHT: Record<string, number> = { body: 1.0, references: 0.2, index: 0.1, contents: 0.1 };
const KIND_CASE =
	'CASE p.kind ' +
	Object.entries(KIND_WEIGHT)
		.map(([k, w]) => `WHEN '${k}' THEN ${w}`)
		.join(' ') +
	' ELSE 1.0 END';

const PAGE_SQL = `
	WITH h AS MATERIALIZED (
		SELECT rowid AS id, bm25(file_pages_fts) AS r FROM file_pages_fts WHERE file_pages_fts MATCH ?),
	s AS (SELECT p.id, p.file_id, p.page, p.kind, -h.r * (${KIND_CASE}) AS score
	      FROM h JOIN file_pages p ON p.id = h.id),
	k AS (SELECT *, row_number() OVER (PARTITION BY file_id ORDER BY score DESC, page) AS n FROM s)
	SELECT file_id, sum(score * (CASE n WHEN 1 THEN 1.0 WHEN 2 THEN 0.5 WHEN 3 THEN 0.25 ELSE 0 END)) AS score,
	       count(*) AS pages,
	       json_group_array(json_object('page', page, 'kind', kind)) FILTER (WHERE n <= 3) AS top
	FROM k GROUP BY file_id ORDER BY score DESC`;

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

/** Run a query whose first parameter is the MATCH expression. */
function runFts<T>(sql: string, q: string, ...params: (string | number)[]): T[] {
	const attempts = looksAdvanced(q) ? [q, ftsQuery(q)] : [ftsQuery(q)];
	for (const a of attempts) {
		if (!a) continue;
		try {
			return all<T>(sql, a, ...params);
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
	meta.forEach((r, i) => {
		out.set(r.id, {
			score: 1 / (RRF_K + i + 1),
			titleHl: r.title_hl?.includes(HL_OPEN) ? r.title_hl : undefined,
			abstractHl: r.abs?.includes(HL_OPEN) ? r.abs : undefined,
			pageHits: [],
			matchingPages: 0
		});
	});
	// Files that belong to no work (unpromoted, personal paperwork) never show up here.
	const files = runFts<{ file_id: number; score: number; pages: number; top: string }>(PAGE_SQL, q);
	let rank = 0;
	const counted = new Set<number>();
	for (const f of files) {
		for (const wid of lib.fileToWorks.get(f.file_id) ?? []) {
			if (counted.has(wid)) continue; // a second file of the same work: its best file counted
			counted.add(wid);
			rank++;
			let m = out.get(wid);
			if (!m) {
				m = { score: 0, pageHits: [], matchingPages: 0 };
				out.set(wid, m);
			}
			m.score += 1 / (RRF_K + rank);
			m.matchingPages = f.pages;
			m.pageHits = (JSON.parse(f.top) as { page: number; kind: string }[])
				.sort((a, b) => a.page - b.page)
				.map((t) => ({ fileId: f.file_id, page: t.page, kind: t.kind, section: null, snippet: '' }));
		}
	}
	return out;
}

/** Snippets and outline sections for the page hits shown (in place). */
export function fillSnippets(q: string, hits: PageHit[]): void {
	if (!hits.length) return;
	const rows = runFts<{ file_id: number; page: number; snip: string; section: string | null }>(
		`SELECT p.file_id, p.page, snippet(file_pages_fts, 0, ?2, ?3, '…', 20) AS snip,
		        (SELECT o.title FROM file_outline o WHERE o.file_id = p.file_id AND o.page <= p.page
		          ORDER BY o.page DESC, o.position DESC LIMIT 1) AS section
		 FROM file_pages_fts JOIN file_pages p ON p.id = file_pages_fts.rowid
		 WHERE file_pages_fts MATCH ?1 AND +file_pages_fts.rowid IN (
		   SELECT p2.id FROM file_pages p2 JOIN json_each(?4) j
		     ON p2.file_id = j.value ->> '$[0]' AND p2.page = j.value ->> '$[1]')`,
		q,
		HL_OPEN,
		HL_CLOSE,
		JSON.stringify(hits.map((h) => [h.fileId, h.page]))
	);
	const byKey = new Map(rows.map((r) => [`${r.file_id}:${r.page}`, r]));
	const adjacent = new RegExp(`${HL_CLOSE}(\\s+)${HL_OPEN}`, 'g'); // one highlight for "Monte Carlo"
	for (const h of hits) {
		const r = byKey.get(`${h.fileId}:${h.page}`);
		h.snippet = (r?.snip ?? '').replace(/\s+/g, ' ').replace(adjacent, '$1');
		h.section = r?.section ?? null;
	}
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
	const now = new Date();
	const valuesOf = new Map<number, Record<string, { value: string; label: string; hint?: string }[]>>();
	for (const w of lib.works) {
		const rec: Record<string, { value: string; label: string; hint?: string }[]> = {};
		for (const fd of FACETS) rec[fd.key] = facetValues(lib, w, fd.key, now);
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
		// Found: all four windows, in their own order, whether or not anything falls in them.
		if (fd.key === 'found')
			FOUND_WINDOWS.forEach(({ value, label, hint }, i) =>
				entries.set(value, { value, label, hint, count: 0, total: -i, selected: false })
			);
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
			.sort((a, b) => (fd.key === 'found' ? 0 : b.total - a.total || a.label.localeCompare(b.label)))
			.map(({ total: _total, ...v }) => v);
		return { key: fd.key, label: fd.label, values, ...(fd.key === 'found' ? { fixed: true, single: true } : {}) };
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

	let rows = base.filter((w) => passAll(w) && matchesText(w, f.match));

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
			pageHits: m?.pageHits.map((h) => ({ ...h })),
			matchingPages: m?.matchingPages
		};
	});
	if (f.q) fillSnippets(f.q, hits.flatMap((h) => h.pageHits ?? []));

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
