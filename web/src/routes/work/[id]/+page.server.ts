import { error } from '@sveltejs/kit';
import { workDetail } from '#lib/server/detail.ts';
import { library } from '#lib/server/library.ts';
import type { PageServerLoad } from './$types';

export const load: PageServerLoad = ({ params, url }) => {
	const detail = workDetail(library(), Number(params.id));
	if (!detail) error(404, 'No such work');
	const fileParam = Number(url.searchParams.get('file'));
	const fileId = detail.work.files.some((f) => f.id === fileParam) ? fileParam : detail.work.primaryFileId;
	const page = Math.max(1, Number(url.searchParams.get('page')) || 1);
	return { ...detail, fileId, page, q: url.searchParams.get('q') ?? '' };
};
