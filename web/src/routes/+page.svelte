<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { onMount, tick } from 'svelte';
	import { collapsed } from '#lib/collapse.svelte.ts';
	import FacetGroup from '#lib/components/FacetGroup.svelte';
	import WorkCard from '#lib/components/WorkCard.svelte';
	import YearHistogram from '#lib/components/YearHistogram.svelte';
	import { href } from '#lib/paths.ts';
	import { hrefWith, matchesText } from '#lib/util.ts';
	import type { PageProps } from './$types';

	let { data }: PageProps = $props();

	const url = $derived({ pathname: page.url.pathname, search: page.url.search });
	const view = $derived((page.url.searchParams.get('view') ?? 'list') as 'list' | 'strip' | 'compact');
	const like = $derived(data.filters.like);
	const facetLimits: Record<string, number> = { kind: 12, status: 4, folder: 6, origin: 6 };
	const anyFilter = $derived(
		Object.keys(data.filters.facets).length > 0 || data.filters.yearFrom != null || data.filters.yearTo != null
	);

	const SORTS: { value: string; label: string; title: string }[] = [
		{ value: 'auto', label: 'Best', title: '' },
		{ value: 'added', label: 'New', title: 'Most recently added (file download time) first' },
		{ value: 'year', label: 'Year', title: 'Newest publication year first' },
		{ value: 'cited', label: 'Citations', title: 'Most cited first' },
		{ value: 'title', label: 'Title', title: 'Alphabetical by title' },
		{ value: 'random', label: 'Random', title: 'Random order (click again to reshuffle)' }
	];
	const sortParam = $derived(page.url.searchParams.get('sort') ?? 'auto');
	const autoLabel = $derived(data.sort === 'similar' ? 'similarity' : data.sort === 'relevance' ? 'relevance' : 'recently added');
	function sortHref(value: string): string {
		if (value === 'random')
			return hrefWith(url, { sort: 'random', seed: sortParam === 'random' ? data.filters.seed + 1 : data.filters.seed });
		return hrefWith(url, { sort: value === 'auto' ? null : value, seed: null });
	}

	// Two fixed columns of filter cards, at every window width.
	// Year leads column A and Kind leads column B. arXiv category is hidden,
	// except while one is selected (from a cs.LG-style chip on a card), so
	// that selection stays visible and can be undone.
	const COL_A = ['subject', 'field', 'author'];
	const COL_B = ['kind', 'folder', 'origin', 'status'];
	const showArxiv = $derived(!!data.filters.facets.arxiv?.length);
	const facetByKey = $derived(new Map(data.facets.map((f) => [f.key, f])));
	// Over the whole library the author distribution is nearly flat (almost
	// everyone has one work), so only offer it once something has narrowed
	// the set, or while an author is selected.
	const showAuthor = $derived(
		Object.keys(data.filters.facets).some((k) => k !== 'author') ||
			data.filters.yearFrom != null ||
			data.filters.yearTo != null ||
			!!data.filters.q ||
			data.filters.like.length > 0 ||
			!!data.filters.facets.author?.length
	);

	onMount(() => collapsed.load());

	// Results sub-filter: applied instantly to the loaded hits, then (debounced)
	// written to ?match= so the server applies it to the whole result set.
	let matchText = $state(page.url.searchParams.get('match') ?? '');
	let matchEl: HTMLInputElement | undefined = $state();
	let matchTimer: ReturnType<typeof setTimeout> | undefined;
	$effect(() => {
		const m = page.url.searchParams.get('match') ?? '';
		if (document.activeElement !== matchEl) matchText = m;
	});
	function commitMatch(delay = 250) {
		clearTimeout(matchTimer);
		matchTimer = setTimeout(() => {
			const v = matchText.trim() ? matchText : null;
			if ((v ?? '') === (page.url.searchParams.get('match') ?? '')) return;
			goto(hrefWith(page.url, { match: v }), { replace: true, reset: false });
		}, delay);
	}
	const hits = $derived(matchText.trim() ? data.hits.filter((h) => matchesText(h, matchText)) : data.hits);

	// Keyboard navigation (j/k, o/Enter, s, x).
	let focused = $state(-1);
	$effect(() => {
		void hits;
		focused = -1;
	});

	async function move(delta: number) {
		const n = hits.length;
		if (!n) return;
		focused = focused < 0 ? (delta > 0 ? 0 : n - 1) : Math.max(0, Math.min(n - 1, focused + delta));
		await tick();
		document.querySelector(`[data-work="${hits[focused].id}"]`)?.scrollIntoView({ block: 'nearest' });
	}

	function onkeydown(e: KeyboardEvent) {
		const t = e.target as HTMLElement;
		if (t.tagName === 'INPUT' || t.tagName === 'SELECT' || t.tagName === 'TEXTAREA' || e.metaKey || e.ctrlKey || e.altKey) return;
		const hit = focused >= 0 ? hits[focused] : undefined;
		switch (e.key) {
			case 'j':
				move(1);
				break;
			case 'k':
				move(-1);
				break;
			case 'o':
			case 'Enter':
				if (hit) goto(href(`/work/${hit.id}${data.filters.q ? `?q=${encodeURIComponent(data.filters.q)}` : ''}`));
				break;
			case 's':
				if (hit) goto(hrefWith(url, { like: hit.id, sort: null }));
				break;
			case 'x':
				if (hit) {
					const next = like.includes(hit.id) ? like.filter((x) => x !== hit.id) : [...like, hit.id];
					goto(hrefWith(url, { like: next.join(',') || null, sort: null }), { reset: false });
				}
				break;
			case 'v':
				goto(hrefWith(url, { view: view === 'list' ? 'strip' : view === 'strip' ? 'compact' : null }), { reset: false });
				break;
			default:
				return;
		}
		e.preventDefault();
	}
</script>

<svelte:window {onkeydown} />

<svelte:head>
	<title>{data.filters.q ? `${data.filters.q} · ` : ''}papyrus</title>
</svelte:head>

<div class="layout">
	<aside>
		<!-- Always occupies its row, so it never pushes the panels down when it appears. -->
		<div class="clear-all">
			<a
				class:hidden={!anyFilter}
				tabindex={anyFilter ? 0 : -1}
				href={hrefWith(url, Object.fromEntries([...Object.keys(data.filters.facets), 'from', 'to'].map((k) => [k, null])))}
				>× clear filters</a
			>
		</div>
		<div class="cards">
			<div class="col">
				<YearHistogram years={data.years} range={data.yearRange} from={data.filters.yearFrom} to={data.filters.yearTo} {url} />
				{#each COL_A as key (key)}
					{@const f = facetByKey.get(key)}
					{#if f && (key !== 'author' || showAuthor)}
						<FacetGroup facet={f} {url} limit={facetLimits[key] ?? 8} />
					{/if}
				{/each}
			</div>
			<div class="col">
				{#each COL_B as key (key)}
					{@const f = facetByKey.get(key)}
					{#if f}<FacetGroup facet={f} {url} limit={facetLimits[key] ?? 8} />{/if}
				{/each}
				<!-- Last, so its appearance never moves the cards above it. -->
				{#if showArxiv && facetByKey.get('arxiv')}
					<FacetGroup facet={facetByKey.get('arxiv')!} {url} limit={8} />
				{/if}
			</div>
		</div>
	</aside>

	<section class="results">
		<div class="toolbar">
			<div class="count">
				{#if matchText.trim() && hits.length < data.hits.length && data.total === data.hits.length}
					<!-- typed but not yet applied server-side: count what is shown -->
					<strong>{hits.length}</strong> {hits.length === 1 ? 'work' : 'works'}
				{:else}
					<strong>{data.total}</strong>
					{data.total === 1 ? 'work' : 'works'}
				{/if}
				{#if data.filters.q}matching <q>{data.filters.q}</q>{/if}
				{#if matchText.trim()}containing <q>{matchText.trim()}</q>{/if}
			</div>
			<div class="controls">
				<div class="match" class:active={!!matchText.trim()}>
					<input
						bind:this={matchEl}
						type="search"
						placeholder="Filter results…"
						aria-label="Filter results by exact text in title, author or abstract"
						title="Exact text in title, author or abstract (case-insensitive)"
						bind:value={matchText}
						oninput={() => commitMatch()}
						onkeydown={(e) => {
							if (e.key === 'Enter') commitMatch(0);
							else if (e.key === 'Escape') {
								if (matchText) {
									matchText = '';
									commitMatch(0);
								} else matchEl?.blur();
							}
						}}
					/>
				</div>
				<div class="seg" role="group" aria-label="Sort">
					{#each SORTS as s (s.value)}
						<a
							class="btn"
							class:active={sortParam === s.value}
							href={sortHref(s.value)}
							title={s.value === 'auto' ? `Best for the current view: ${autoLabel}` : s.title}
							>{s.label}{#if s.value === 'random' && sortParam === 'random'}&nbsp;⟳{/if}</a
						>
					{/each}
				</div>
				<div class="seg" role="group" aria-label="View">
					<a class="btn" class:active={view === 'list'} href={hrefWith(url, { view: null })} title="List (v)">List</a>
					<a class="btn" class:active={view === 'strip'} href={hrefWith(url, { view: 'strip' })} title="Page strips, arxiv-sanity style (v)">Pages</a>
					<a class="btn" class:active={view === 'compact'} href={hrefWith(url, { view: 'compact' })} title="Compact (v)">Compact</a>
				</div>
			</div>
		</div>

		{#if data.seeds.length}
			<div class="seeds">
				<span class="muted">{data.seeds.length === 1 ? 'Similar to' : 'More like these'}:</span>
				{#each data.seeds as s (s.id)}
					<span class="seed">
						<a href={href(`/work/${s.id}`)}>{s.title ?? `#${s.id}`}</a>
						<a class="x" href={hrefWith(url, { like: like.filter((x) => x !== s.id).join(',') || null })} title="Remove">×</a>
					</span>
				{/each}
				<a class="clear" href={hrefWith(url, { like: null, sort: null })}>clear</a>
				<span class="faint">· TF-IDF cosine over titles, abstracts, summaries and topics</span>
			</div>
		{/if}

		{#if hits.length === 0}
			<div class="empty">
				<p>Nothing matches.</p>
				{#if data.filters.q}
					<p class="muted">
						Full-text search covers the pages extracted so far (mostly the first two pages of each PDF). Try fewer words, or
						FTS5 syntax such as <code>"state space" NEAR/5 mamba</code>.
					</p>
				{/if}
			</div>
		{:else}
			<div class="list">
				{#each hits as hit, i (hit.id)}
					<WorkCard {hit} {url} {view} focused={i === focused} {like} sort={data.sort} match={matchText} />
				{/each}
			</div>
			{#if data.total > data.hits.length}
				<div class="more">
					<a class="btn" href={hrefWith(url, { limit: data.filters.limit + 60 })} data-sveltekit-noscroll
						>Show more ({data.total - data.hits.length} left)</a
					>
				</div>
			{/if}
		{/if}

		<footer class="keys faint">
			<kbd>/</kbd> search · <kbd>j</kbd>/<kbd>k</kbd> move · <kbd>o</kbd> open · <kbd>s</kbd> similar · <kbd>x</kbd> add
			to “more like these” · <kbd>v</kbd> view
		</footer>
	</section>
</div>

<style>
	/* Two columns of filter cards, narrower in narrower windows (196px at
	   ~1500px and below, 252px from ~1940px); the results take the rest. */
	.layout {
		--card: clamp(196px, 13vw, 252px);
		--card-gap: 10px;
		--cols: 2;
		display: grid;
		/* Left-aligned; the results column takes all remaining width (capped
		   only on very wide screens, where lines would get unreadably long). */
		grid-template-columns:
			calc(var(--cols) * var(--card) + (var(--cols) - 1) * var(--card-gap))
			minmax(0, 1fr);
		justify-content: start;
		gap: 24px;
		padding: 18px 22px 40px;
	}
	aside {
		position: sticky;
		top: 70px;
		align-self: start;
		max-height: calc(100vh - 88px);
		/* Always show the scrollbar track (scrollbar-gutter alone is not
		   honoured everywhere), so cards never narrow when the list grows. */
		overflow-y: scroll;
		scrollbar-gutter: stable;
	}
	/* Fixed columns, no flowing: each card stays in its column whatever
	   happens to the others. One column = A stacked above B. */
	.cards {
		display: grid;
		grid-template-columns: repeat(var(--cols), minmax(0, 1fr));
		column-gap: var(--card-gap);
		align-items: start;
	}
	.col {
		min-width: 0;
	}
	.clear-all {
		height: 18px;
		font-size: 12px;
		margin-bottom: 8px;
		padding-left: 6px;
	}
	.clear-all .hidden {
		visibility: hidden;
	}
	.results {
		min-width: 0;
		max-width: 2000px;
	}
	.match {
		/* Its 80px basis decides wrapping; then it grows into the spare width, up to 190px. */
		flex: 1 1 80px;
		max-width: 190px;
	}
	.match input {
		width: 100%;
		padding: 4px 10px;
		font-size: 12.5px;
		background: var(--panel);
	}
	.match.active input {
		border-color: color-mix(in srgb, var(--accent) 55%, transparent);
		background: #16233a;
	}
	/* One row: the filter box shrinks first; past that the view switch wraps under the sorts. */
	.toolbar {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px 12px;
		padding: 0 16px 10px;
		border-bottom: 1px solid var(--border);
	}
	.count {
		color: var(--muted);
		min-width: 0;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.count {
		flex: none;
	}
	.controls {
		flex: 1 1 0;
		min-width: 0;
	}
	.controls .seg :global(.btn) {
		padding: 3px 8px;
	}
	.count strong {
		color: var(--text-strong);
	}
	.count q {
		color: var(--text);
	}
	.controls {
		display: flex;
		flex-wrap: wrap;
		justify-content: flex-end;
		align-items: center;
		gap: 8px 10px;
	}
	.seeds {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 8px;
		margin: 12px 16px 4px;
		padding: 8px 12px;
		background: var(--accent-dim);
		border: 1px solid color-mix(in srgb, var(--accent) 35%, transparent);
		border-radius: var(--radius);
		font-size: 12.5px;
	}
	.seed {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		background: var(--panel);
		border-radius: 5px;
		padding: 1px 4px 1px 8px;
		max-width: 360px;
	}
	.seed a:first-child {
		color: var(--text-strong);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.seed .x {
		color: var(--muted);
		padding: 0 4px;
	}
	.clear {
		font-size: 12px;
	}
	.list {
		display: flex;
		flex-direction: column;
		margin-top: 6px;
	}
	.empty {
		padding: 40px 16px;
		color: var(--text);
	}
	.empty code {
		font-family: var(--mono);
		font-size: 12px;
		background: var(--panel);
		padding: 1px 5px;
		border-radius: 4px;
	}
	.more {
		display: flex;
		justify-content: center;
		padding: 18px;
	}
	.keys {
		margin-top: 28px;
		padding: 0 16px;
		font-size: 11.5px;
	}
</style>
