import { createReadStream } from 'node:fs';
import { Readable } from 'node:stream';
import { error } from '@sveltejs/kit';
import { resolveFile } from '#lib/server/files.ts';
import type { RequestHandler } from './$types';

// Streams a PDF that the database knows about, with HTTP Range support so
// the browser's viewer can open large books without reading them whole.
export const GET: RequestHandler = ({ params, request, url }) => {
	const file = resolveFile(Number(params.id));
	if (!file) error(404, 'File not found on disk');

	const disposition = url.searchParams.has('download') ? 'attachment' : 'inline';
	const headers: Record<string, string> = {
		'content-type': 'application/pdf',
		'accept-ranges': 'bytes',
		'content-disposition': `${disposition}; filename*=UTF-8''${encodeURIComponent(file.filename)}`,
		'cache-control': 'private, max-age=600',
		etag: `"${file.sha256}"`
	};

	if (request.headers.get('if-none-match') === headers.etag) return new Response(null, { status: 304, headers });

	const range = /^bytes=(\d*)-(\d*)$/.exec(request.headers.get('range') ?? '');
	if (range && (range[1] || range[2])) {
		let start = range[1] ? Number(range[1]) : file.size - Number(range[2]);
		let end = range[1] && range[2] ? Number(range[2]) : file.size - 1;
		start = Math.max(0, start);
		end = Math.min(end, file.size - 1);
		if (start > end) return new Response(null, { status: 416, headers: { 'content-range': `bytes */${file.size}` } });
		const body = Readable.toWeb(createReadStream(file.path, { start, end })) as ReadableStream;
		return new Response(body, {
			status: 206,
			headers: { ...headers, 'content-range': `bytes ${start}-${end}/${file.size}`, 'content-length': String(end - start + 1) }
		});
	}

	const body = Readable.toWeb(createReadStream(file.path)) as ReadableStream;
	return new Response(body, { headers: { ...headers, 'content-length': String(file.size) } });
};
