import { readFile } from 'node:fs/promises';
import { error } from '@sveltejs/kit';
import { resolveFile, thumbnail } from '#lib/server/files.ts';
import type { RequestHandler } from './$types';

const WIDTHS = [160, 320, 480, 720];

export const GET: RequestHandler = async ({ params, url }) => {
	const file = resolveFile(Number(params.id));
	if (!file) error(404, 'File not found on disk');
	const page = Number(params.page);
	if (!Number.isInteger(page) || page < 1 || (file.pageCount != null && page > file.pageCount))
		error(404, 'No such page');
	const requested = Number(url.searchParams.get('w') ?? 320);
	const width = WIDTHS.find((w) => w >= requested) ?? WIDTHS.at(-1)!;
	let png: Buffer;
	try {
		png = await readFile(await thumbnail(file, page, width));
	} catch (e) {
		error(500, `Could not render page: ${(e as Error).message}`);
	}
	return new Response(new Uint8Array(png), {
		headers: { 'content-type': 'image/png', 'cache-control': 'private, max-age=86400, immutable' }
	});
};
