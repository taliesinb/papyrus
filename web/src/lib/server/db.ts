// Read-only access to the papyrus SQLite database.
//
// The Python pipeline (scan / triage / enrich) may be writing to the same
// file at any time. We open it with SQLITE_OPEN_READONLY plus
// `PRAGMA query_only`, so this process can never take a write lock; in WAL
// mode readers never block the writer and always see the last commit.

import { DatabaseSync, type SQLInputValue, type StatementSync } from 'node:sqlite';
import { homedir } from 'node:os';
import { join } from 'node:path';

/** The variant this server shows (`PAPYRUS_VARIANT`, set by Papyrus Server.app), default `release`. */
export function variant(): string {
	const v = process.env.PAPYRUS_VARIANT?.trim() || 'release';
	if (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(v)) throw new Error(`PAPYRUS_VARIANT=${v} is not a variant id`);
	return v;
}

/** The library: `PAPYRUS_HOME`, else the variant's home `<PAPYRUS_ROOT or ~/.papyrus>/<variant>` (as src/papyrus/db.py). */
export function libraryHome(): string {
	return process.env.PAPYRUS_HOME || join(process.env.PAPYRUS_ROOT || join(homedir(), '.papyrus'), variant());
}

/** What titles call this library: `papyrus`, or `papyrus personal` for the variant `personal`. */
export function brand(): string {
	const v = variant();
	return v === 'release' ? 'papyrus' : `papyrus ${v.replaceAll('-', ' ')}`;
}

export function dbPath(): string {
	return process.env.PAPYRUS_DB ?? join(libraryHome(), 'papyrus.sqlite');
}

let conn: DatabaseSync | null = null;
const stmts = new Map<string, StatementSync>();

export function db(): DatabaseSync {
	if (!conn) {
		conn = new DatabaseSync(dbPath(), { readOnly: true, timeout: 5000 });
		conn.exec('PRAGMA query_only = ON');
	}
	return conn;
}

function stmt(sql: string): StatementSync {
	let s = stmts.get(sql);
	if (!s) {
		s = db().prepare(sql);
		stmts.set(sql, s);
	}
	return s;
}

/** Rows as plain objects (node:sqlite returns null-prototype objects). */
export function all<T>(sql: string, ...params: SQLInputValue[]): T[] {
	return stmt(sql)
		.all(...params)
		.map((r) => ({ ...r }) as T);
}

export function get<T>(sql: string, ...params: SQLInputValue[]): T | undefined {
	const r = stmt(sql).get(...params);
	return r ? ({ ...r } as T) : undefined;
}

/**
 * Changes whenever another connection commits to the database. Used to
 * invalidate in-memory caches and to tell the browser the library changed.
 */
export function dataVersion(): number {
	return Number(get<{ data_version: number }>('PRAGMA data_version')!.data_version);
}

export function parseJson<T>(s: string | null | undefined, fallback: T): T {
	if (s == null) return fallback;
	try {
		return JSON.parse(s) as T;
	} catch {
		return fallback;
	}
}
