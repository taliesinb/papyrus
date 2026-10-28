<script lang="ts">
	import { href } from '#lib/paths.ts';
	import type { WorkHit } from '#lib/types.ts';
	import { formatDate, hrefToggle, hrefWith, markAll, markExcerpt, primaryIds } from '#lib/util.ts';
	import Hl from './Hl.svelte';
	import IdChip from './IdChip.svelte';
	import KindBadge from './KindBadge.svelte';
	import Thumb from './Thumb.svelte';

	let {
		hit,
		url,
		view = 'list',
		focused = false,
		like = [],
		sort,
		match = ''
	}: {
		hit: WorkHit;
		url: { pathname: string; search: string };
		view?: 'list' | 'strip' | 'compact';
		focused?: boolean;
		like?: number[];
		sort: string;
		/** Results sub-filter text; its occurrences are highlighted (taking precedence over search highlights). */
		match?: string;
	} = $props();

	let expanded = $state(false);

	const q = $derived(new URLSearchParams(url.search).get('q') ?? '');
	const workHref = (extra: Record<string, string | number> = {}) => {
		const sp = new URLSearchParams();
		if (q) sp.set('q', q);
		for (const [k, v] of Object.entries(extra)) sp.set(k, String(v));
		const s = sp.toString();
		return href(`/work/${hit.id}${s ? `?${s}` : ''}`);
	};
	const inLike = $derived(like.includes(hit.id));
	const likeHref = $derived(
		hrefWith(url, {
			like: (inLike ? like.filter((x) => x !== hit.id) : [...like, hit.id]).join(',') || null,
			sort: null
		}, href('/'))
	);
	const arxivCats = $derived(hit.topics.filter((t) => t.scheme === 'arxiv' && t.level === 'topic').slice(0, 4));
	const llmTopics = $derived(hit.topics.filter((t) => t.scheme === 'llm' && t.level === 'topic').slice(0, 5));
	const stripPages = $derived(Array.from({ length: Math.min(hit.pageCount ?? 1, 8) }, (_, i) => i + 1));
	const blurb = $derived(hit.abstract ?? hit.summary);

	const m = $derived(match.trim());
	const titleText = $derived(
		m && hit.title?.toLowerCase().includes(m.toLowerCase()) ? markAll(hit.title, m) : (hit.titleHl ?? null)
	);
	// Collapsed: start the excerpt near the first match so it is visible within the 3-line clamp.
	const blurbMatch = $derived(m && blurb ? (expanded ? markAll(blurb, m) : markExcerpt(blurb, m)) : null);
	const authorText = (name: string) => (m ? markAll(name, m) : name);
	const ids = $derived(primaryIds(hit.ids).slice(0, 3));

	// The id chips sit right of the title, before the kind badge. They show their value
	// (arXiv 1903.05848) only while the title still fits on one line beside them;
	// otherwise just the scheme (arXiv), so they never push the title onto another line.
	// The title is inline, so its client rects are its line boxes.
	function fitIds(row: HTMLElement) {
		const title = row.querySelector<HTMLElement>('a.title');
		if (!title) return;
		const fit = () => {
			row.classList.remove('ids-short');
			if (title.getClientRects().length > 1) row.classList.add('ids-short');
		};
		const observer = new ResizeObserver(fit);
		observer.observe(row);
		fit();
		return () => observer.disconnect();
	}
</script>

{#snippet actions()}
	{#if hit.score != null && sort === 'similar'}
		<span class="score" title="cosine similarity">{hit.score.toFixed(2)}</span>
	{/if}
	<a
		class="btn ghost small icon"
		href={hrefWith(url, { like: hit.id, sort: null }, href('/'))}
		title="Similar: rank the library by similarity to this (s)"
		aria-label="Similar"
		><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2 9.2c1.6-2.6 3.6-2.6 6 0s4.4 2.6 6 0" /></svg></a
	>
	<a
		class="btn ghost small icon"
		class:active={inLike}
		href={likeHref}
		title={inLike ? 'Remove from the “more like these” set (x)' : 'Add to the “more like these” set (x)'}
		aria-label={inLike ? 'Remove from more like these' : 'More like these'}
		><svg viewBox="0 0 16 16" aria-hidden="true"><path d={inLike ? 'M3.5 8h9' : 'M8 3.5v9M3.5 8h9'} /></svg></a
	>
{/snippet}

<article class="card v-{view}" class:focused data-work={hit.id}>
	{#if view === 'list'}
		<a class="cover" href={workHref()} tabindex="-1" aria-hidden="true">
			<Thumb fileId={hit.primaryFileId} width={160} />
		</a>
	{/if}
	<div class="body">
		<div class="title-row ids-short" {@attach fitIds}>
			<div class="title-text">
				<a class="title" href={workHref()}>
					{#if titleText}<Hl text={titleText} />{:else}{hit.title ?? 'Untitled'}{/if}
				</a>
			</div>
			<div class="badges">
				{#each ids as id (id.scheme + id.value)}
					<IdChip scheme={id.scheme} value={id.value} />
				{/each}
				<KindBadge kind={hit.kind} small />
			</div>
		</div>
		<div class="meta">
			{#if hit.authors.length}
				<span class="authors">
					{#each hit.authors.slice(0, 8) as a, i (i)}
						<a
							class="author"
							href={hrefToggle(url, 'author', a.personId != null ? `p${a.personId}` : `n:${a.name}`)}
							title="Filter by {a.name}"><Hl text={authorText(a.name)} /></a
						>{#if i < Math.min(hit.authors.length, 8) - 1},&nbsp;{/if}
					{/each}
					{#if hit.authors.length > 8}<span class="muted">&nbsp;+{hit.authors.length - 8}</span>{/if}
				</span>
			{/if}
			{#if hit.year}<span class="dot">·</span><span class="fixed">{hit.year}</span>{/if}
			{#if hit.venue && hit.venue !== 'arXiv'}<span class="dot">·</span><span class="venue" class:long={hit.venue.length > 12} title={hit.venue}>{hit.venue}</span>{/if}
			{#if hit.pageCount}<span class="dot">·</span><span class="muted fixed">{hit.pageCount} pp</span>{/if}
			{#if hit.addedAt}
				<span class="added" title="Found {formatDate(hit.addedAt, true)} in {hit.folders.join(', ')}"
					>{#if hit.origins.length}{hit.origins[0]}&nbsp;· {/if}{formatDate(hit.addedAt)}</span
				>
			{/if}
			{#if view === 'compact'}<span class="actions">{@render actions()}</span>{/if}
		</div>

		{#if view !== 'compact'}
			{#if hit.abstractHl && !blurbMatch}
				<p class="abstract hl"><Hl text={hit.abstractHl} /></p>
			{:else if blurb}
				<button
					type="button"
					class="abstract"
					class:expanded
					onclick={() => (expanded = !expanded)}
					title={(hit.abstract ? '' : 'Summary by the local model (no abstract found). ') + (expanded ? 'Collapse' : 'Expand')}
				>
					{#if blurbMatch}<Hl text={blurbMatch} />{:else}{blurb}{/if}
				</button>
			{/if}
		{/if}

		{#if hit.pageHits?.length}
			<ul class="page-hits">
				{#each hit.pageHits as ph (ph.fileId + ':' + ph.page)}
					<li>
						<a href={workHref({ file: ph.fileId, page: ph.page })}>p.{ph.page}</a>
						<span><Hl text={ph.snippet} /></span>
					</li>
				{/each}
			</ul>
		{/if}

		{#if view === 'strip'}
			<div class="strip">
				{#each stripPages as p (p)}
					<a href={workHref({ page: p })} title="Page {p}"><Thumb fileId={hit.primaryFileId} page={p} width={160} /></a>
				{/each}
			</div>
		{/if}

		{#if view !== 'compact'}
			<div class="foot">
				<!-- One line of tags: those that do not fit are hidden, not wrapped. -->
				<div class="tags">
					{#each arxivCats as t (t.id)}
						<a class="tag cat" href={hrefToggle(url, 'arxiv', t.name)}>{t.name}</a>
					{/each}
					{#each llmTopics as t (t.id)}
						<span class="tag">{t.name}</span>
					{/each}
				</div>
				{@render actions()}
			</div>
		{/if}
	</div>
</article>

<style>
	.card {
		display: grid;
		grid-template-columns: 92px 1fr;
		gap: 16px;
		padding: 14px 16px;
		border: 1px solid transparent;
		border-bottom-color: var(--border);
		border-radius: var(--radius);
		transition: background 0.1s;
	}
	.card.v-strip {
		grid-template-columns: 1fr;
	}
	.card.v-compact {
		grid-template-columns: 1fr;
		padding: 8px 16px;
	}
	.card:hover {
		background: var(--bg-raised);
	}
	.card.focused {
		background: var(--bg-raised);
		border-color: var(--border-strong);
		box-shadow: inset 3px 0 0 var(--accent);
	}
	.cover {
		display: block;
		align-self: start;
	}
	.body {
		min-width: 0;
	}
	.title-row {
		display: flex;
		align-items: flex-start;
		gap: 12px;
	}
	.title-text {
		flex: 1 1 auto;
		min-width: 0;
	}
	.badges {
		flex: none;
		display: flex;
		align-items: center;
		gap: 6px;
		height: 22px; /* the title's first line */
	}
	.v-compact .badges {
		height: 19.5px;
	}
	.title-row.ids-short :global(.chip .v) {
		display: none;
	}
	.badges :global(.chip) {
		line-height: 16px;
		font-size: 11px;
	}
	.badges :global(.badge) {
		vertical-align: 0;
	}
	.title {
		font-family: var(--serif);
		font-size: 17px;
		line-height: 1.3;
		color: var(--text-strong);
		font-weight: 600;
	}
	.v-compact .title {
		font-size: 15px;
	}
	.title:hover {
		color: var(--accent);
		text-decoration: none;
	}
	/* One line: the authors and the venue give way (ellipsis) before anything wraps. */
	.meta {
		display: flex;
		flex-wrap: nowrap;
		align-items: baseline;
		gap: 0 6px;
		margin-top: 4px;
		font-size: 12.5px;
		color: var(--text);
		white-space: nowrap;
	}
	.meta > * {
		flex: none;
	}
	.meta > .authors {
		flex: 0 1 auto;
		min-width: 4em;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.meta > .venue.long {
		/* Gives way long before the authors do. */
		flex: 0 100 auto;
		min-width: 5em;
	}
	.meta > .actions {
		display: flex;
		gap: 2px;
		align-self: center;
		margin-left: 6px;
	}
	.author {
		color: var(--text);
	}
	.author:hover {
		color: var(--accent);
	}
	.dot {
		color: var(--faint);
	}
	.venue {
		color: var(--muted);
		font-style: italic;
		max-width: 340px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.abstract {
		display: -webkit-box;
		-webkit-line-clamp: 3;
		line-clamp: 3;
		-webkit-box-orient: vertical;
		overflow: hidden;
		margin: 8px 0 0;
		padding: 0;
		border: 0;
		background: none;
		text-align: left;
		color: #b4bcc8;
		font-size: 13px;
		line-height: 1.55;
		cursor: pointer;
	}
	.abstract.expanded {
		-webkit-line-clamp: unset;
		line-clamp: unset;
	}
	.abstract.hl {
		cursor: default;
	}
	.page-hits {
		list-style: none;
		margin: 8px 0 0;
		padding: 0;
		font-size: 12.5px;
		color: #b4bcc8;
	}
	.page-hits li {
		display: flex;
		gap: 8px;
		padding: 2px 0;
	}
	.page-hits a {
		font-family: var(--mono);
		font-size: 11.5px;
		min-width: 34px;
		flex: none;
	}
	.strip {
		display: grid;
		grid-template-columns: repeat(8, minmax(0, 1fr));
		max-width: 1100px; /* don't let page thumbnails balloon on wide screens */
		gap: 6px;
		margin-top: 10px;
	}
	.foot {
		display: flex;
		align-items: center;
		gap: 4px;
		margin-top: 8px;
	}
	.tags {
		flex: 1 1 auto;
		min-width: 0;
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
		height: 19px;
		overflow: hidden;
	}
	.tag {
		white-space: nowrap;
	}
	.tag {
		font-size: 11.5px;
		color: var(--muted);
		background: var(--panel);
		border-radius: 4px;
		padding: 0 6px;
		line-height: 19px;
	}
	.tag.cat {
		font-family: var(--mono);
		font-size: 11px;
		color: var(--accent-2);
	}
	.tag.cat:hover {
		text-decoration: none;
		background: var(--panel-2);
	}
	.added {
		margin-left: auto;
		padding-left: 12px;
		font-size: 11.5px;
		color: var(--faint);
	}
	.score {
		font-family: var(--mono);
		font-size: 11px;
		color: var(--good);
	}
	.btn.small {
		font-size: 11.5px;
		padding: 1px 7px;
	}
	.btn.icon {
		flex: none;
		width: 24px;
		height: 22px;
		padding: 0;
		justify-content: center;
	}
	.btn.icon svg {
		width: 14px;
		height: 14px;
		fill: none;
		stroke: currentColor;
		stroke-width: 1.7;
		stroke-linecap: round;
	}
</style>
