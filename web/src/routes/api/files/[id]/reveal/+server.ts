import { execFile } from 'node:child_process';
import { error } from '@sveltejs/kit';
import { resolveFile } from '#lib/server/files.ts';
import type { RequestHandler } from './$types';

// Reveal the file in Finder (macOS). Only paths recorded in the database.
export const POST: RequestHandler = ({ params }) => {
	const file = resolveFile(Number(params.id));
	if (!file) error(404, 'File not found on disk');
	if (process.platform !== 'darwin') error(501, 'Reveal is only supported on macOS');
	execFile('/usr/bin/open', ['-R', file.path]);
	return new Response(null, { status: 204 });
};
