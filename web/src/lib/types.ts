// Shared (client-safe) types for the papyrus web UI.

export interface Author {
	name: string;
	personId: number | null;
	orcid: string | null;
	openalexId: string | null;
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
	citations: number | null;
}

/** A work as shown in a result list, plus search/similarity annotations. */
export interface WorkHit extends WorkSummary {
	score?: number;
	titleHl?: string;
	abstractHl?: string;
	pageHits?: PageHit[];
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
}
