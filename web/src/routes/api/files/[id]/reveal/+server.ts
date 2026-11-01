import { error } from '@sveltejs/kit';
import { resolveFile, reveal, revealLabel } from '#lib/server/files.ts';
import type { RequestHandler } from './$types';

// Show the file in the server machine's file manager (Finder; a FileManager1 one on Linux).
// Only paths recorded in the database.
export const POST: RequestHandler = ({ params }) => {
	const file = resolveFile(Number(params.id));
	if (!file) error(404, 'File not found on disk');
	if (!revealLabel()) error(501, 'No file manager on the server to show the file in');
	reveal(file.path);
	return new Response(null, { status: 204 });
};
