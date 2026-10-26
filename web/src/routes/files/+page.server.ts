import { basename } from 'node:path';
import { all, parseJson } from '#lib/server/db.ts';
import { fileAvailable } from '#lib/server/files.ts';
import { library } from '#lib/server/library.ts';
import type { PageServerLoad } from './$types';

interface FileRow {
	id: number;
	filename: string | null;
	folder: string | null;
	paths: number;
	pageCount: number | null;
	size: number;
	textStatus: string;
	triageStatus: string;
	addedAt: string | null;
	kind: string | null;
	kindP: number | null;
	library: boolean | null;
	works: { id: number; title: string | null; status: string }[];
	available: boolean;
}

export const load: PageServerLoad = () => {
	const lib = library();
	const ctx = new Map<number, { locator: string; occurred_at: string | null }[]>();
	for (const r of all<{ file_id: number; locator: string; occurred_at: string | null }>(
		`SELECT r.file_id, c.locator, c.occurred_at FROM context_refs r JOIN contexts c ON c.id = r.context_id
		 WHERE r.file_id IS NOT NULL AND c.kind = 'filesystem' ORDER BY c.occurred_at`
	)) {
		const l = ctx.get(r.file_id) ?? [];
		l.push(r);
		ctx.set(r.file_id, l);
	}
	const claims = new Map<number, { kind?: string; kindP?: number; lib?: boolean }>();
	for (const r of all<{ subject_id: number; field: string; value: string; confidence: number | null }>(
		`SELECT subject_id, field, value, confidence FROM assertions
		 WHERE subject_type = 'file' AND field IN ('kind', 'is_library_material') AND superseded_at IS NULL
		 ORDER BY created_at`
	)) {
		const c = claims.get(r.subject_id) ?? {};
		if (r.field === 'kind') {
			c.kind = parseJson<string>(r.value, r.value);
			c.kindP = r.confidence ?? undefined;
		} else c.lib = parseJson<boolean>(r.value, false);
		claims.set(r.subject_id, c);
	}
	const home = process.env.HOME ?? '';
	const files: FileRow[] = all<{
		id: number;
		page_count: number | null;
		size: number;
		text_status: string;
		triage_status: string;
	}>(`SELECT id, page_count, size, text_status, triage_status FROM files ORDER BY id`).map((f) => {
		const cs = ctx.get(f.id) ?? [];
		const first = cs[0];
		const dir = first ? first.locator.slice(0, first.locator.lastIndexOf('/')) : null;
		const c = claims.get(f.id) ?? {};
		return {
			id: f.id,
			filename: first ? basename(first.locator) : null,
			folder: dir ? (dir.startsWith(home + '/') ? dir.slice(home.length + 1) : dir) : null,
			paths: cs.length,
			pageCount: f.page_count,
			size: f.size,
			textStatus: f.text_status,
			triageStatus: f.triage_status,
			addedAt: first?.occurred_at ?? null,
			kind: c.kind ?? null,
			kindP: c.kindP ?? null,
			library: c.lib ?? null,
			works: (lib.fileToWorks.get(f.id) ?? []).map((wid) => {
				const w = lib.byId.get(wid);
				return { id: wid, title: w?.title ?? null, status: w?.status ?? '?' };
			}),
			available: fileAvailable(f.id)
		};
	});
	return { files };
};
