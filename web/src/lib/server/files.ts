// Locating PDFs on disk and rendering page thumbnails.
//
// Only paths recorded in the database are ever served: a file id maps to
// papyrus's managed copy (files.stored_path) or to a live filesystem
// context. We check the size matches so a different file that has since
// taken the same path is not served by mistake.
//
// Only files of live works (candidate or confirmed) are served at all: the
// library also knows personal paperwork that the classifier kept out (payslips, CVs;
// waiting for review, or purged down to a hash), and file ids are easy to guess.

import { execFile, spawnSync } from 'node:child_process';
import { accessSync, constants, existsSync, mkdirSync, renameSync, rmSync, statSync } from 'node:fs';
import { homedir } from 'node:os';
import { basename, delimiter, join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
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

/** SQL: the file whose id is the expression `col` belongs to a live work (as search.LIVE_FILE in Python). */
export const LIVE_FILE = (col: string) =>
	`EXISTS (SELECT 1 FROM file_works fw JOIN works w ON w.id = fw.work_id
	         WHERE fw.file_id = ${col} AND w.status IN ('candidate', 'confirmed'))`;

export function resolveFile(id: number): ResolvedFile | null {
	const f = get<{ sha256: string; size: number; stored_path: string | null; page_count: number | null }>(
		`SELECT sha256, size, stored_path, page_count FROM files f WHERE id = ? AND ${LIVE_FILE('f.id')}`,
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

/** Show a file in the server machine's file manager: Finder, or on Linux whichever implements
 * org.freedesktop.FileManager1 (Nautilus, Dolphin, Nemo, Thunar, …) on the session bus. */
export function reveal(path: string): void {
	if (process.platform === 'darwin') {
		execFile('/usr/bin/open', ['-R', path], ignore);
		return;
	}
	const uri = pathToFileURL(path).href;
	execFile('gdbus', ['call', '--session', '--dest', 'org.freedesktop.FileManager1', '--object-path',
		'/org/freedesktop/FileManager1', '--method', 'org.freedesktop.FileManager1.ShowItems', `['${uri.replace(/'/g, '%27')}']`, ''], ignore);
}

// With a callback, a missing program is an error passed to it rather than an uncaught 'error' event.
const ignore = () => {};

/** The Reveal button's label, or null where there is no file manager to show it in. */
export function revealLabel(): string | null {
	if (process.platform === 'darwin') return 'Reveal in Finder';
	// A desktop session: a headless server (no session bus) has no file manager to show the file in.
	if (process.platform === 'linux' && process.env.DBUS_SESSION_BUS_ADDRESS && (process.env.DISPLAY || process.env.WAYLAND_DISPLAY))
		return 'Show in Files';
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
// Page thumbnails, cached outside the library directory.
//
// Two renderers, the same page at the same width either way:
//  - papyrus-thumb (macOS, CoreGraphics; apps/macos/Sources/Thumb). Papyrus Server.app passes the
//    copy in its bundle as PAPYRUS_THUMB; a server started from web/ (`pnpm dev`, `pnpm start`)
//    finds apps/macos/.build/papyrus-thumb, which `python3 apps/macos/build.py` links.
//  - poppler's pdftoppm (Linux; macOS without the above). PDFTOPPM, else PATH and the usual
//    places (a systemd unit's PATH is short). Linux packages: poppler-utils (Debian, Ubuntu,
//    Fedora, nixpkgs), poppler (Arch, Homebrew).

export function cacheDir(): string {
	return process.env.PAPYRUS_WEB_CACHE ?? join(homedir(), '.cache', 'papyrus-web');
}

export interface Renderer {
	kind: 'papyrus-thumb' | 'pdftoppm';
	path: string;
	/** pdftoppm: has -scale-dimension-before-rotation (poppler 0.86+). */
	scaleBeforeRotation?: boolean;
}

function executable(p: string): boolean {
	try {
		accessSync(p, constants.X_OK);
		return statSync(p).isFile();
	} catch {
		return false;
	}
}

let found: Renderer | undefined;

/** The page renderer, or null when there is none (looked for again on the next call). */
export function renderer(): Renderer | null {
	if (found) return found;
	const thumb = [process.env.PAPYRUS_THUMB, process.platform === 'darwin' ? resolve('../apps/macos/.build/papyrus-thumb') : undefined];
	for (const p of thumb) if (p && executable(p)) return (found = { kind: 'papyrus-thumb', path: p });
	const dirs = [...(process.env.PATH ?? '').split(delimiter), '/opt/homebrew/bin', '/usr/local/bin', '/usr/bin',
		'/run/current-system/sw/bin', join(homedir(), '.nix-profile', 'bin')];
	const poppler = [process.env.PDFTOPPM, ...dirs.filter(Boolean).map((d) => join(d, 'pdftoppm'))];
	for (const p of poppler) {
		if (!p || !executable(p)) continue;
		const help = spawnSync(p, ['-h'], { encoding: 'utf8', timeout: 5_000 });
		const scaleBeforeRotation = `${help.stdout}${help.stderr}`.includes('-scale-dimension-before-rotation');
		return (found = { kind: 'pdftoppm', path: p, scaleBeforeRotation });
	}
	return null;
}

const NO_RENDERER =
	process.platform === 'darwin'
		? 'no page renderer: Papyrus Server.app carries papyrus-thumb; from web/, run python3 apps/macos/build.py once (or install poppler)'
		: 'no page renderer: install poppler (pdftoppm; Debian/Ubuntu/Fedora: poppler-utils, Arch: poppler) or set PDFTOPPM';

/** Render one page to `png`. */
async function render(file: ResolvedFile, page: number, width: number, png: string): Promise<void> {
	const r = renderer();
	if (!r) throw new Error(NO_RENDERER);
	if (r.kind === 'papyrus-thumb') {
		await run(r.path, [file.path, String(page), String(width), png], { timeout: 60_000 });
		return;
	}
	// pdftoppm adds the .png itself. Scaling before rotation keeps a /Rotate'd page `width` wide
	// (without it, -scale-to-x applies to the unrotated page: 320 asked, 415 given).
	await run(
		r.path,
		['-f', String(page), '-l', String(page), '-scale-to-x', String(width), '-scale-to-y', '-1',
			...(r.scaleBeforeRotation ? ['-scale-dimension-before-rotation'] : []),
			'-png', '-singlefile', file.path, png.replace(/\.png$/, '')],
		{ timeout: 60_000 }
	);
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
			const tmp = join(dir, `.tmp-${process.pid}-${Math.random().toString(36).slice(2)}.png`);
			try {
				await render(file, page, width, tmp);
				renameSync(tmp, out);
			} finally {
				rmSync(tmp, { force: true });
			}
			return out;
		}).finally(() => inflight.delete(out));
		inflight.set(out, p);
	}
	return p;
}
