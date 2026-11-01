// Shared (client-safe) types for the papyrus web UI.

export interface Author {
	name: string;
	personId: number | null;
	orcid: string | null;
	openalexId: string | null;
	/** Linked to the person only by an unambiguous name match, on the read side (library.ts). */
	byName?: boolean;
}

export interface Ident {
	scheme: string;
	value: string;
}

export interface Topic {
	id: number;
	scheme: string; // llm | arxiv | openalex | user
	level: string | null; // domain | field | subfield | topic
	name: string;
	parent: string | null;
	parentId: number | null;
	score: number | null;
}

export interface FileRef {
	id: number;
	relation: string;
	version: string | null;
	pageCount: number | null;
	size: number;
	textStatus: string;
	filename: string | null;
	available: boolean;
}

export interface PageHit {
	fileId: number;
	page: number;
	/** body | references | index | contents */
	kind: string;
	/** The PDF outline entry the page belongs to. */
	section: string | null;
	snippet: string; // \u0002 … \u0003 mark highlighted spans
}

export interface WorkSummary {
	id: number;
	kind: string | null;
	status: string;
	title: string | null;
	subtitle: string | null;
	year: number | null;
	venue: string | null;
	language: string | null;
	abstract: string | null;
	summary: string | null;
	authors: Author[];
	ids: Ident[];
	topics: Topic[];
	files: FileRef[];
	primaryFileId: number | null;
	pageCount: number | null;
	addedAt: string | null; // earliest discovery (file birth time)
	folders: string[]; // e.g. Desktop, Downloads
	origins: string[]; // download hosts from kMDItemWhereFroms
	source: WorkSource | null; // the earliest discovery
	citations: number | null;
}

/** Where a work was first found: a URL (download link), else a path on disk. */
export interface WorkSource {
	url: string | null;
	path: string | null;
	/** The URL's host, or the path's folder relative to home. */
	label: string;
	fileId: number;
}

/** A work as shown in a result list, plus search/similarity annotations. */
export interface WorkHit extends WorkSummary {
	score?: number;
	titleHl?: string;
	abstractHl?: string;
	pageHits?: PageHit[];
	/** How many pages match in all (pageHits holds the best few). */
	matchingPages?: number;
}

export interface FacetValue {
	value: string;
	label: string;
	count: number;
	selected: boolean;
	hint?: string;
}

export interface Facet {
	key: string;
	label: string;
	values: FacetValue[];
	/** A fixed list (Found): every value is shown, in this order, even with a zero count. */
	fixed?: boolean;
	/** Single-choice (Found): selecting a value replaces the selection. */
	single?: boolean;
}

export interface ContextInfo {
	id: number;
	kind: string;
	locator: string;
	source: string | null;
	occurredAt: string | null;
	missingAt: string | null;
	url: string | null;
	whereFroms: string[];
	downloadAgent: string | null;
	fileId: number | null;
	text: string | null;
	actor: string | null;
}

export interface AssertionRow {
	id: number;
	subjectType: string;
	subjectId: number;
	field: string;
	value: string;
	confidence: number | null;
	source: string;
	createdAt: string;
	superseded: boolean;
}

export interface PipelineStatus {
	running: { id: number; command: string; backend: string | null; startedAt: string }[];
	last: { id: number; command: string; finishedAt: string | null; stats: string | null } | null;
	counts: { works: number; files: number; people: number; pages: number };
	version: string;
	/** `papyrus watch` (src/papyrus/watch/daemon.py): its <home>/watch.json, or null if none runs. */
	watch: WatchStatus | null;
}

export interface WatchStatus {
	state: 'starting' | 'idle' | 'ingesting' | 'waiting-memory' | 'lmstudio-unavailable';
	detail: string;
	retryAt: string | null;
	pending: { triage: number; extract: number; enrich: number } | null;
}
