// Client-safe helpers.

type Changes = Record<string, string | number | (string | number)[] | null | undefined>;

/** A relative href for `url` with some query parameters replaced or removed. */
export function hrefWith(url: URL | { pathname: string; search: string }, changes: Changes, path?: string): string {
	const sp = new URLSearchParams(url.search);
	for (const [k, v] of Object.entries(changes)) {
		sp.delete(k);
		if (v == null || v === '') continue;
		if (Array.isArray(v)) for (const x of v) sp.append(k, String(x));
		else sp.set(k, String(v));
	}
	// Changing anything other than the limit resets paging.
	if (!('limit' in changes)) sp.delete('limit');
	const qs = sp.toString();
	return (path ?? url.pathname) + (qs ? `?${qs}` : '');
}

/** Toggle one value of a repeated query parameter. */
export function hrefToggle(url: URL | { pathname: string; search: string }, key: string, value: string): string {
	const sp = new URLSearchParams(url.search);
	const vs = sp.getAll(key);
	return hrefWith(url, { [key]: vs.includes(value) ? vs.filter((v) => v !== value) : [...vs, value] });
}

export function formatDate(iso: string | null | undefined, withTime = false): string {
	if (!iso) return '';
	const d = new Date(iso);
	if (Number.isNaN(d.getTime())) return iso;
	return d.toLocaleDateString('en-GB', {
		day: 'numeric',
		month: 'short',
		year: 'numeric',
		...(withTime ? { hour: '2-digit', minute: '2-digit' } : {})
	});
}

export function relativeTime(iso: string | null | undefined): string {
	if (!iso) return '';
	const s = (Date.now() - new Date(iso).getTime()) / 1000;
	if (s < 60) return 'just now';
	if (s < 3600) return `${Math.round(s / 60)} min ago`;
	if (s < 86400) return `${Math.round(s / 3600)} h ago`;
	if (s < 86400 * 30) return `${Math.round(s / 86400)} d ago`;
	return formatDate(iso);
}

export function formatSize(bytes: number): string {
	if (bytes < 1024) return `${bytes} B`;
	if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(0)} KB`;
	return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
}

const ID_LABELS: Record<string, string> = {
	arxiv: 'arXiv',
	doi: 'DOI',
	isbn: 'ISBN',
	openalex: 'OpenAlex',
	s2: 'S2',
	s2_corpus: 'S2 corpus',
	dblp: 'DBLP',
	pmid: 'PubMed',
	mag: 'MAG',
	url: 'URL'
};

export function idLabel(scheme: string): string {
	return ID_LABELS[scheme] ?? scheme;
}

export function idLink(scheme: string, value: string): string | null {
	switch (scheme) {
		case 'arxiv':
			return `https://arxiv.org/abs/${value}`;
		case 'doi':
			return `https://doi.org/${value}`;
		case 'isbn':
			return `https://openlibrary.org/isbn/${value.replace(/[^0-9Xx]/g, '')}`;
		case 'openalex':
			return `https://openalex.org/works/${value}`;
		case 's2':
			return `https://www.semanticscholar.org/paper/${value}`;
		case 's2_corpus':
			return `https://www.semanticscholar.org/p/${value}`;
		case 'dblp':
			return `https://dblp.org/rec/${value}`;
		case 'pmid':
			return `https://pubmed.ncbi.nlm.nih.gov/${value}`;
		case 'url':
			return value;
	}
	return null;
}

/** Identifiers worth showing as chips, most useful first. */
export function primaryIds<T extends { scheme: string }>(ids: T[]): T[] {
	const order = ['arxiv', 'doi', 'isbn', 'pmid', 'dblp'];
	return ids
		.filter((i) => order.includes(i.scheme))
		.sort((a, b) => order.indexOf(a.scheme) - order.indexOf(b.scheme));
}

export function authorLine(authors: { name: string }[], max = 6): string {
	if (authors.length <= max) return authors.map((a) => a.name).join(', ');
	return authors.slice(0, max).map((a) => a.name).join(', ') + ` +${authors.length - max}`;
}

export function hostOf(url: string): string {
	try {
		return new URL(url).hostname.replace(/^www\./, '');
	} catch {
		return url;
	}
}
