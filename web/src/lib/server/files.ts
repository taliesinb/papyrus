// Locating PDFs on disk and rendering page thumbnails.
//
// Only paths recorded in the database are ever served: a file id maps to
// papyrus's managed copy (files.stored_path) or to a live filesystem
// context. We check the size matches so a different file that has since
// taken the same path is not served by mistake.

import { execFile } from 'node:child_process';
import { existsSync, mkdirSync, renameSync, statSync } from 'node:fs';
import { homedir } from 'node:os';
import { basename, join } from 'node:path';
import { promisify } from 'node:util';
import { all, get, libraryHome } from './db.ts';

const run = promisify(execFile);

export interface ResolvedFile {
	id: number;
	path: string;
	filename: string;
	size: number;
	sha256: string;
	pageCount: number | null;
}

export function resolveFile(id: number): ResolvedFile | null {
	const f = get<{ sha256: string; size: number; stored_path: string | null; page_count: number | null }>(
		`SELECT sha256, size, stored_path, page_count FROM files WHERE id = ?`,
		id
	);
	if (!f) return null;
	const candidates: string[] = [];
	if (f.stored_path) candidates.push(join(libraryHome(), f.stored_path));
	for (const c of all<{ locator: string }>(
		`SELECT c.locator FROM context_refs r JOIN contexts c ON c.id = r.context_id
		 WHERE r.file_id = ? AND c.kind = 'filesystem' AND c.missing_at IS NULL
		 ORDER BY c.occurred_at`,
		id
	))
		candidates.push(c.locator);
	for (const p of candidates) {
		try {
			const st = statSync(p);
			if (st.isFile() && st.size === f.size)
				return { id, path: p, filename: basename(p), size: st.size, sha256: f.sha256, pageCount: f.page_count };
		} catch {
			/* moved or deleted since the last scan */
		}
	}
	return null;
}

const availability = new Map<number, { ok: boolean; at: number }>();

/** Cheap, cached (30 s) check that a file can be served. */
export function fileAvailable(id: number): boolean {
	const hit = availability.get(id);
	if (hit && Date.now() - hit.at < 30_000) return hit.ok;
	const ok = resolveFile(id) != null;
	availability.set(id, { ok, at: Date.now() });
	return ok;
}

// ---------------------------------------------------------------------------
// Thumbnails (poppler's pdftoppm), cached outside the library directory.

export function cacheDir(): string {
	return process.env.PAPYRUS_WEB_CACHE ?? join(homedir(), '.cache', 'papyrus-web');
}

function pdftoppm(): string {
	if (process.env.PDFTOPPM) return process.env.PDFTOPPM;
	for (const p of ['/opt/homebrew/bin/pdftoppm', '/usr/local/bin/pdftoppm', '/usr/bin/pdftoppm'])
		if (existsSync(p)) return p;
	return 'pdftoppm';
}

const MAX_JOBS = 3;
let active = 0;
const queue: (() => void)[] = [];
const inflight = new Map<string, Promise<string>>();

async function withSlot<T>(fn: () => Promise<T>): Promise<T> {
	if (active >= MAX_JOBS) await new Promise<void>((r) => queue.push(r));
	active++;
	try {
		return await fn();
	} finally {
		active--;
		queue.shift()?.();
	}
}

/** Path to a PNG of `page` (1-based) at `width` px, rendering it if needed. */
export function thumbnail(file: ResolvedFile, page: number, width: number): Promise<string> {
	const dir = join(cacheDir(), 'thumbs');
	const out = join(dir, `${file.sha256.slice(0, 32)}-p${page}-w${width}.png`);
	if (existsSync(out)) return Promise.resolve(out);
	let p = inflight.get(out);
	if (!p) {
		p = withSlot(async () => {
			mkdirSync(dir, { recursive: true });
			const tmpBase = join(dir, `.tmp-${process.pid}-${Math.random().toString(36).slice(2)}`);
			await run(
				pdftoppm(),
				['-f', String(page), '-l', String(page), '-scale-to-x', String(width), '-scale-to-y', '-1',
					'-png', '-singlefile', file.path, tmpBase],
				{ timeout: 60_000 }
			);
			renameSync(tmpBase + '.png', out);
			return out;
		}).finally(() => inflight.delete(out));
		inflight.set(out, p);
	}
	return p;
}
