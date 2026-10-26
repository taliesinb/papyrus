import { library } from '#lib/server/library.ts';
import { parseFilters, runQuery } from '#lib/server/query.ts';
import type { PageServerLoad } from './$types';

export const load: PageServerLoad = ({ url }) => {
	const filters = parseFilters(url);
	const result = runQuery(library(), filters);
	return { filters, ...result };
};
