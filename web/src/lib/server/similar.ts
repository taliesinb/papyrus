// TF-IDF similarity over title + abstract + summary + topics, in the spirit
// of arxiv-sanity: unigrams and bigrams, sublinear tf, l2-normalised
// vectors, cosine similarity. "More like these" ranks by the centroid of
// several works' vectors (a cheap stand-in for arxiv-sanity's per-tag SVM).

import type { WorkSummary } from '#lib/types.ts';

type Vec = Map<string, number>;

export interface TfidfIndex {
	vectors: Map<number, Vec>;
	idf: Map<string, number>;
}

const STOP = new Set(
	`a about above after again against all also am an and any are as at be because been before being below
	between both but by can could did do does doing down during each few for from further had has have having
	he her here hers herself him himself his how i if in into is it its itself just me more most my myself no
	nor not now of off on once only or other our ours ourselves out over own same she should so some such than
	that the their theirs them themselves then there these they this those through to too under until up very
	was we were what when where which while who whom why will with would you your yours yourself yourselves
	via using use used based new show shows shown paper papers propose proposed approach approaches method
	methods result results work works present presents study studies however thus within without across
	well many much one two three first second may can cannot often provide provides allow allows enable
	enables introduce introduces given various several different make makes made way ways also et al`
		.split(/\s+/)
		.filter(Boolean)
);

function stem(t: string): string {
	if (t.length > 4 && t.endsWith('ies')) return t.slice(0, -3) + 'y';
	if (t.length > 4 && t.endsWith('s') && !t.endsWith('ss') && !t.endsWith('us') && !t.endsWith('is'))
		return t.slice(0, -1);
	return t;
}

export function tokenize(text: string): string[] {
	const words = text
		.toLowerCase()
		.normalize('NFKD')
		.replace(/[\u0300-\u036f]/g, '')
		.match(/[a-z][a-z0-9]*(?:-[a-z0-9]+)*/g);
	if (!words) return [];
	const toks = words.filter((w) => w.length > 2 && !STOP.has(w)).map(stem);
	const out = [...toks];
	for (let i = 0; i + 1 < toks.length; i++) out.push(toks[i] + ' ' + toks[i + 1]);
	return out;
}

function docText(w: WorkSummary): string {
	const topicNames = w.topics.filter((t) => t.scheme !== 'arxiv').map((t) => t.name);
	return [
		w.title,
		w.title, // titles are short; weight them up
		w.subtitle,
		w.abstract,
		w.summary,
		topicNames.join('. '),
		topicNames.join('. ')
	]
		.filter(Boolean)
		.join('. ');
}

export function buildTfidf(works: WorkSummary[]): TfidfIndex {
	const tfs = new Map<number, Map<string, number>>();
	const df = new Map<string, number>();
	for (const w of works) {
		const tf = new Map<string, number>();
		for (const t of tokenize(docText(w))) tf.set(t, (tf.get(t) ?? 0) + 1);
		// arXiv categories are strong signals; add them as single opaque terms.
		for (const t of w.topics) if (t.scheme === 'arxiv') tf.set('cat:' + t.name, 2);
		tfs.set(w.id, tf);
		for (const t of tf.keys()) df.set(t, (df.get(t) ?? 0) + 1);
	}
	const n = works.length;
	const idf = new Map<string, number>();
	for (const [t, d] of df) {
		// Terms in more than half the library carry no information.
		if (n > 10 && d > n / 2) continue;
		idf.set(t, Math.log((1 + n) / (1 + d)) + 1);
	}
	const vectors = new Map<number, Vec>();
	for (const [id, tf] of tfs) {
		const v: Vec = new Map();
		let norm = 0;
		for (const [t, c] of tf) {
			const i = idf.get(t);
			if (!i) continue;
			const x = (1 + Math.log(c)) * i;
			v.set(t, x);
			norm += x * x;
		}
		norm = Math.sqrt(norm) || 1;
		for (const [t, x] of v) v.set(t, x / norm);
		vectors.set(id, v);
	}
	return { vectors, idf };
}

function dot(a: Vec, b: Vec): number {
	const [s, l] = a.size < b.size ? [a, b] : [b, a];
	let d = 0;
	for (const [t, x] of s) {
		const y = l.get(t);
		if (y) d += x * y;
	}
	return d;
}

function centroid(index: TfidfIndex, ids: number[]): Vec {
	const c: Vec = new Map();
	for (const id of ids) {
		const v = index.vectors.get(id);
		if (!v) continue;
		for (const [t, x] of v) c.set(t, (c.get(t) ?? 0) + x / ids.length);
	}
	return c;
}

/** Cosine similarity of every work to the centroid of `ids`. */
export function similarity(index: TfidfIndex, ids: number[]): Map<number, number> {
	const c = centroid(index, ids);
	let norm = 0;
	for (const x of c.values()) norm += x * x;
	norm = Math.sqrt(norm) || 1;
	const out = new Map<number, number>();
	for (const [id, v] of index.vectors) out.set(id, dot(c, v) / norm);
	return out;
}

/** The terms contributing most to the similarity between two works. */
export function sharedTerms(index: TfidfIndex, a: number, b: number, n = 5): string[] {
	const va = index.vectors.get(a);
	const vb = index.vectors.get(b);
	if (!va || !vb) return [];
	return [...va]
		.filter(([t]) => vb.has(t))
		.map(([t, x]) => [t, x * vb.get(t)!] as const)
		.sort((p, q) => q[1] - p[1])
		.slice(0, n)
		.map(([t]) => t.replace(/^cat:/, ''));
}

/** A work's most distinctive terms (arxiv-sanity's "inspect" view). */
export function topTerms(index: TfidfIndex, id: number, n = 16): { term: string; weight: number }[] {
	const v = index.vectors.get(id);
	if (!v) return [];
	return [...v]
		.sort((a, b) => b[1] - a[1])
		.slice(0, n)
		.map(([term, weight]) => ({ term: term.replace(/^cat:/, ''), weight }));
}
