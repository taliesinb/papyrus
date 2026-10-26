// In-memory view of the library, rebuilt whenever the database changes.
//
// A personal library is at most a few thousand works, so loading the
// resolved view once and faceting/sorting in JS is simpler and faster than
// composing dynamic SQL. Full-text search still goes through SQLite FTS5.

import { homedir } from 'node:os';
import { basename, dirname } from 'node:path';
import type { Author, FileRef, Ident, Topic, WorkSummary } from '#lib/types.ts';
import { all, dataVersion, parseJson } from './db.ts';
import { buildTfidf, type TfidfIndex } from './similar.ts';
import { fileAvailable } from './files.ts';

export interface TopicNode {
	id: number;
	scheme: string;
	level: string | null;
	name: string;
	parentId: number | null;
	externalId: string | null;
}

export interface Library {
	version: number;
	works: WorkSummary[];
	byId: Map<number, WorkSummary>;
	fileToWorks: Map<number, number[]>;
	topics: Map<number, TopicNode>;
	tfidf: TfidfIndex;
	builtAt: number;
}

/** A topic and all its ancestors (topic → subfield → field → domain). */
export function lineage(lib: Library, topicId: number): TopicNode[] {
	const out: TopicNode[] = [];
	let t = lib.topics.get(topicId);
	while (t && out.length < 8) {
		out.push(t);
		t = t.parentId != null ? lib.topics.get(t.parentId) : undefined;
	}
	return out;
}

let cache: Library | null = null;

export function library(): Library {
	const v = dataVersion();
	if (!cache || cache.version !== v) cache = build(v);
	return cache;
}

const RELATION_RANK: Record<string, number> = {
	published: 0,
	full: 1,
	preprint: 2,
	excerpt: 3,
	slides_for: 4,
	supplement: 5
};

function folderOf(path: string): string {
	const home = homedir();
	const dir = dirname(path);
	if (dir.startsWith(home + '/')) return dir.slice(home.length + 1);
	return dir;
}

function hostOf(url: string): string | null {
	try {
		return new URL(url).hostname.replace(/^www\./, '');
	} catch {
		return null;
	}
}

function build(version: number): Library {
	const works = all<{
		id: number;
		kind: string | null;
		title: string | null;
		subtitle: string | null;
		abstract: string | null;
		year: number | null;
		venue: string | null;
		language: string | null;
		status: string;
	}>(
		`SELECT id, kind, title, subtitle, abstract, year, venue, language, status
		 FROM works WHERE status <> 'merged'`
	);

	const authors = new Map<number, Author[]>();
	for (const r of all<{
		work_id: number;
		raw_name: string;
		person_id: number | null;
		display_name: string | null;
		orcid: string | null;
		openalex_id: string | null;
	}>(
		`SELECT wa.work_id, wa.raw_name, wa.person_id, p.display_name, p.orcid, p.openalex_id
		 FROM work_authors wa LEFT JOIN people p ON p.id = wa.person_id
		 WHERE wa.role = 'author' ORDER BY wa.work_id, wa.position`
	)) {
		const list = authors.get(r.work_id) ?? [];
		list.push({
			name: r.display_name ?? r.raw_name,
			personId: r.person_id,
			orcid: r.orcid,
			openalexId: r.openalex_id
		});
		authors.set(r.work_id, list);
	}

	const ids = new Map<number, Ident[]>();
	for (const r of all<{ work_id: number; scheme: string; value: string }>(
		`SELECT work_id, scheme, value FROM work_ids ORDER BY work_id, scheme`
	)) {
		const list = ids.get(r.work_id) ?? [];
		list.push({ scheme: r.scheme, value: r.value });
		ids.set(r.work_id, list);
	}

	const topics = new Map<number, Topic[]>();
	for (const r of all<{
		work_id: number;
		id: number;
		scheme: string;
		level: string | null;
		name: string;
		parent: string | null;
		parent_id: number | null;
		score: number | null;
	}>(
		`SELECT wt.work_id, t.id, t.scheme, t.level, t.name, p.name AS parent, t.parent_id, wt.score
		 FROM work_topics wt JOIN topics t ON t.id = wt.topic_id
		 LEFT JOIN topics p ON p.id = t.parent_id
		 ORDER BY wt.work_id, wt.score DESC`
	)) {
		const list = topics.get(r.work_id) ?? [];
		list.push({
			id: r.id,
			scheme: r.scheme,
			level: r.level,
			name: r.name,
			parent: r.parent,
			parentId: r.parent_id,
			score: r.score
		});
		topics.set(r.work_id, list);
	}

	// Discovery contexts per file (filesystem paths, download URLs).
	const fileCtx = new Map<
		number,
		{ locator: string; occurredAt: string | null; missing: boolean; whereFroms: string[] }[]
	>();
	for (const r of all<{
		file_id: number;
		kind: string;
		locator: string;
		occurred_at: string | null;
		missing_at: string | null;
		extra: string | null;
	}>(
		`SELECT r.file_id, c.kind, c.locator, c.occurred_at, c.missing_at, c.extra
		 FROM context_refs r JOIN contexts c ON c.id = r.context_id
		 WHERE r.file_id IS NOT NULL ORDER BY c.occurred_at`
	)) {
		const extra = parseJson<{ where_froms?: string[] }>(r.extra, {});
		const list = fileCtx.get(r.file_id) ?? [];
		list.push({
			locator: r.kind === 'filesystem' ? r.locator : '',
			occurredAt: r.occurred_at,
			missing: r.missing_at != null,
			whereFroms: extra.where_froms ?? []
		});
		fileCtx.set(r.file_id, list);
	}

	const files = new Map<number, FileRef[]>();
	const fileToWorks = new Map<number, number[]>();
	for (const r of all<{
		work_id: number;
		file_id: number;
		relation: string;
		version: string | null;
		page_count: number | null;
		size: number;
		text_status: string;
	}>(
		`SELECT fw.work_id, fw.file_id, fw.relation, fw.version, f.page_count, f.size, f.text_status
		 FROM file_works fw JOIN files f ON f.id = fw.file_id`
	)) {
		const ctx = fileCtx.get(r.file_id) ?? [];
		const live = ctx.find((c) => c.locator && !c.missing);
		const list = files.get(r.work_id) ?? [];
		list.push({
			id: r.file_id,
			relation: r.relation,
			version: r.version,
			pageCount: r.page_count,
			size: r.size,
			textStatus: r.text_status,
			filename: live ? basename(live.locator) : ctx[0]?.locator ? basename(ctx[0].locator) : null,
			available: fileAvailable(r.file_id)
		});
		files.set(r.work_id, list);
		const ws = fileToWorks.get(r.file_id) ?? [];
		ws.push(r.work_id);
		fileToWorks.set(r.file_id, ws);
	}

	// Latest live summary per work (falls back to the file-level triage summary).
	const summaries = new Map<number, string>();
	for (const r of all<{ subject_type: string; subject_id: number; value: string }>(
		`SELECT subject_type, subject_id, value FROM assertions
		 WHERE field = 'summary' AND superseded_at IS NULL
		 ORDER BY subject_type DESC, created_at`
	)) {
		// subject_type DESC puts 'work' before 'file'; later rows of the same type win.
		const v = parseJson<string | null>(r.value, null);
		if (!v) continue;
		if (r.subject_type === 'work') summaries.set(r.subject_id, v);
		else
			for (const w of fileToWorks.get(r.subject_id) ?? [])
				if (!summaries.has(w)) summaries.set(w, v);
	}

	const citations = new Map<number, number>();
	for (const r of all<{ subject_id: number; n: number }>(
		`SELECT subject_id, MAX(CAST(value AS INTEGER)) AS n FROM assertions
		 WHERE subject_type = 'work' AND field = 'citation_count' AND superseded_at IS NULL
		 GROUP BY subject_id`
	))
		citations.set(r.subject_id, r.n);

	const out: WorkSummary[] = works.map((w) => {
		const fs = (files.get(w.id) ?? []).sort(
			(a, b) =>
				Number(b.available) - Number(a.available) ||
				(RELATION_RANK[a.relation] ?? 9) - (RELATION_RANK[b.relation] ?? 9) ||
				(b.pageCount ?? 0) - (a.pageCount ?? 0)
		);
		const ctxs = fs.flatMap((f) => fileCtx.get(f.id) ?? []);
		const dates = ctxs.map((c) => c.occurredAt).filter((d): d is string => !!d).sort();
		const folders = [...new Set(ctxs.filter((c) => c.locator).map((c) => folderOf(c.locator)))];
		const origins = [
			...new Set(
				ctxs
					.map((c) => c.whereFroms[0])
					.filter(Boolean)
					.map((u) => hostOf(u))
					.filter((h): h is string => !!h)
			)
		];
		return {
			...w,
			summary: summaries.get(w.id) ?? null,
			authors: authors.get(w.id) ?? [],
			ids: ids.get(w.id) ?? [],
			topics: topics.get(w.id) ?? [],
			files: fs,
			primaryFileId: fs[0]?.id ?? null,
			pageCount: fs[0]?.pageCount ?? null,
			addedAt: dates[0] ?? null,
			folders,
			origins,
			citations: citations.get(w.id) ?? null
		};
	});

	return {
		version,
		works: out,
		byId: new Map(out.map((w) => [w.id, w])),
		fileToWorks,
		topics: new Map(
			all<{ id: number; scheme: string; level: string | null; name: string; parent_id: number | null; external_id: string | null }>(
				`SELECT id, scheme, level, name, parent_id, external_id FROM topics`
			).map((t) => [
				t.id,
				{ id: t.id, scheme: t.scheme, level: t.level, name: t.name, parentId: t.parent_id, externalId: t.external_id }
			])
		),
		tfidf: buildTfidf(out),
		builtAt: Date.now()
	};
}
