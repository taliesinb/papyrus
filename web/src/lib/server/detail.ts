import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import type { AssertionRow, ContextInfo, PipelineStatus, WatchStage, WatchStatus, WorkSummary } from '#lib/types.ts';
import { all, dataVersion, get, libraryHome, parseJson } from './db.ts';
import type { Library } from './library.ts';
import { sharedTerms, similarity, topTerms } from './similar.ts';

export interface FileMeta {
	id: number;
	sha256: string;
	size: number;
	pageCount: number | null;
	pdfTitle: string | null;
	pdfAuthor: string | null;
	pdfCreator: string | null;
	pdfProducer: string | null;
	pdfCreated: string | null;
	textStatus: string;
	triageStatus: string;
	firstSeenAt: string;
}

export interface WorkDetail {
	work: WorkSummary;
	contexts: ContextInfo[];
	fileMeta: FileMeta[];
	assertions: AssertionRow[];
	similar: { work: WorkSummary; score: number; terms: string[] }[];
	terms: { term: string; weight: number }[];
	pagesWithText: Record<number, number>;
}

export function contextsForFiles(fileIds: number[]): ContextInfo[] {
	if (!fileIds.length) return [];
	const rows = all<{
		id: number;
		kind: string;
		locator: string;
		source: string | null;
		occurred_at: string | null;
		missing_at: string | null;
		url: string | null;
		extra: string | null;
		file_id: number | null;
		text: string | null;
		actor: string | null;
	}>(
		`SELECT c.id, c.kind, c.locator, c.source, c.occurred_at, c.missing_at, c.url, c.extra,
		        r.file_id, c.text, c.actor
		 FROM context_refs r JOIN contexts c ON c.id = r.context_id
		 WHERE r.file_id IN (SELECT value FROM json_each(?))
		 ORDER BY c.occurred_at`,
		JSON.stringify(fileIds)
	);
	return rows.map((r) => {
		const extra = parseJson<{ where_froms?: string[]; download_agent?: string }>(r.extra, {});
		return {
			id: r.id,
			kind: r.kind,
			locator: r.locator,
			source: r.source,
			occurredAt: r.occurred_at,
			missingAt: r.missing_at,
			url: r.url,
			whereFroms: extra.where_froms ?? [],
			downloadAgent: extra.download_agent ?? null,
			fileId: r.file_id,
			text: r.text,
			actor: r.actor
		};
	});
}

export function workDetail(lib: Library, id: number): WorkDetail | null {
	const work = lib.byId.get(id);
	if (!work) return null;
	const fileIds = work.files.map((f) => f.id);

	const fileMeta = all<{
		id: number;
		sha256: string;
		size: number;
		page_count: number | null;
		pdf_title: string | null;
		pdf_author: string | null;
		pdf_creator: string | null;
		pdf_producer: string | null;
		pdf_created: string | null;
		text_status: string;
		triage_status: string;
		first_seen_at: string;
	}>(
		`SELECT id, sha256, size, page_count, pdf_title, pdf_author, pdf_creator, pdf_producer, pdf_created,
		        text_status, triage_status, first_seen_at
		 FROM files WHERE id IN (SELECT value FROM json_each(?))`,
		JSON.stringify(fileIds)
	).map((r) => ({
		id: r.id,
		sha256: r.sha256,
		size: r.size,
		pageCount: r.page_count,
		pdfTitle: r.pdf_title,
		pdfAuthor: r.pdf_author,
		pdfCreator: r.pdf_creator,
		pdfProducer: r.pdf_producer,
		pdfCreated: r.pdf_created,
		textStatus: r.text_status,
		triageStatus: r.triage_status,
		firstSeenAt: r.first_seen_at
	}));

	const assertions = all<{
		id: number;
		subject_type: string;
		subject_id: number;
		field: string;
		value: string;
		confidence: number | null;
		source: string;
		created_at: string;
		superseded_at: string | null;
	}>(
		`SELECT id, subject_type, subject_id, field, value, confidence, source, created_at, superseded_at
		 FROM assertions
		 WHERE (subject_type = 'work' AND subject_id = ?)
		    OR (subject_type = 'file' AND subject_id IN (SELECT value FROM json_each(?)))
		 ORDER BY field, superseded_at IS NOT NULL, created_at DESC`,
		id,
		JSON.stringify(fileIds)
	).map((r) => ({
		id: r.id,
		subjectType: r.subject_type,
		subjectId: r.subject_id,
		field: r.field,
		value: r.value,
		confidence: r.confidence,
		source: r.source,
		createdAt: r.created_at,
		superseded: r.superseded_at != null
	}));

	const sims = similarity(lib.tfidf, [id]);
	const similar = [...sims]
		.filter(([wid]) => wid !== id && lib.byId.get(wid)?.status !== 'rejected')
		.sort((a, b) => b[1] - a[1])
		.slice(0, 8)
		.filter(([, s]) => s > 0.02)
		.map(([wid, score]) => ({ work: lib.byId.get(wid)!, score, terms: sharedTerms(lib.tfidf, id, wid) }));

	const pagesWithText: Record<number, number> = {};
	for (const r of all<{ file_id: number; n: number }>(
		`SELECT file_id, COUNT(*) AS n FROM file_pages WHERE file_id IN (SELECT value FROM json_each(?)) GROUP BY file_id`,
		JSON.stringify(fileIds)
	))
		pagesWithText[r.file_id] = r.n;

	return {
		work,
		contexts: contextsForFiles(fileIds),
		fileMeta,
		assertions,
		similar,
		terms: topTerms(lib.tfidf, id),
		pagesWithText
	};
}

/** Header status: is the pipeline running right now, and how big is the library. */
export function pipelineStatus(): PipelineStatus {
	const running = all<{ id: number; command: string; backend: string | null; started_at: string }>(
		`SELECT id, command, backend, started_at FROM runs
		 WHERE finished_at IS NULL AND started_at > strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-6 hours')
		 ORDER BY id DESC`
	).map((r) => ({ id: r.id, command: r.command, backend: r.backend, startedAt: r.started_at }));
	const last = get<{ id: number; command: string; finished_at: string | null; stats: string | null }>(
		`SELECT id, command, finished_at, stats FROM runs WHERE finished_at IS NOT NULL ORDER BY finished_at DESC LIMIT 1`
	);
	const counts = get<{ works: number; files: number; people: number; pages: number }>(
		`SELECT (SELECT COUNT(*) FROM works WHERE status NOT IN ('rejected', 'merged')) AS works,
		        (SELECT COUNT(*) FROM files) AS files,
		        (SELECT COUNT(*) FROM people) AS people,
		        (SELECT COUNT(*) FROM file_pages) AS pages`
	)!;
	return {
		running,
		last: last ? { id: last.id, command: last.command, finishedAt: last.finished_at, stats: last.stats } : null,
		counts,
		version: String(dataVersion()),
		watch: watchStatus()
	};
}

/** The watcher's status file, if its process is alive (a watcher that crashed leaves the file behind). */
export function watchStatus(): WatchStatus | null {
	let s: {
		state?: string;
		detail?: string;
		pid?: number;
		retry_at?: string;
		pending?: WatchStatus['pending'];
		failed?: number;
		eta_s?: number | null;
		paused?: boolean;
		stages?: {
			stage: string;
			label: string;
			unit: string;
			pending: number;
			state: WatchStage['state'];
			reason?: string;
			per_item_s: number | null;
			eta_s: number | null;
		}[];
	};
	try {
		s = JSON.parse(readFileSync(join(libraryHome(), 'watch.json'), 'utf8'));
	} catch {
		return null;
	}
	if (!s.pid || !s.state || s.state === 'stopped') return null;
	try {
		process.kill(s.pid, 0);
	} catch (e) {
		if ((e as NodeJS.ErrnoException).code !== 'EPERM') return null;
	}
	return {
		state: s.state as WatchStatus['state'],
		detail: s.detail ?? '',
		retryAt: s.retry_at ?? null,
		pending: s.pending ?? null,
		failed: s.failed ?? 0,
		etaS: s.eta_s ?? null,
		paused: s.paused ?? false,
		stages: (s.stages ?? []).map((g) => ({
			stage: g.stage,
			label: g.label,
			unit: g.unit,
			pending: g.pending,
			state: g.state,
			reason: g.reason,
			perItemS: g.per_item_s,
			etaS: g.eta_s
		}))
	};
}
