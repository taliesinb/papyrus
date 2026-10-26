<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { tick } from 'svelte';
	import FacetGroup from '#lib/components/FacetGroup.svelte';
	import WorkCard from '#lib/components/WorkCard.svelte';
	import YearHistogram from '#lib/components/YearHistogram.svelte';
	import { hrefWith } from '#lib/util.ts';
	import type { PageProps } from './$types';

	let { data }: PageProps = $props();

	const url = $derived({ pathname: page.url.pathname, search: page.url.search });
	const view = $derived((page.url.searchParams.get('view') ?? 'list') as 'list' | 'strip' | 'compact');
	const like = $derived(data.filters.like);
	const facetLimits: Record<string, number> = { kind: 12, status: 4, folder: 6, origin: 6 };
	const anyFilter = $derived(
		Object.keys(data.filters.facets).length > 0 || data.filters.yearFrom != null || data.filters.yearTo != null
	);

	const SORTS: { value: string; label: string }[] = [
		{ value: 'auto', label: 'Best' },
		{ value: 'added', label: 'Recently added' },
		{ value: 'year', label: 'Year' },
		{ value: 'cited', label: 'Most cited' },
		{ value: 'title', label: 'Title' },
		{ value: 'random', label: 'Random' }
	];
	const sortParam = $derived(page.url.searchParams.get('sort') ?? 'auto');
	const autoLabel = $derived(data.sort === 'similar' ? 'similarity' : data.sort === 'relevance' ? 'relevance' : 'recently added');

	function setSort(value: string) {
		goto(hrefWith(url, { sort: value === 'auto' ? null : value, seed: value === 'random' ? data.filters.seed : null }), {
			reset: false
		});
	}

	// Keyboard navigation (j/k, o/Enter, s, x).
	let focused = $state(-1);
	$effect(() => {
		void data.hits;
		focused = -1;
	});

	async function move(delta: number) {
		const n = data.hits.length;
		if (!n) return;
		focused = focused < 0 ? (delta > 0 ? 0 : n - 1) : Math.max(0, Math.min(n - 1, focused + delta));
		await tick();
		document.querySelector(`[data-work="${data.hits[focused].id}"]`)?.scrollIntoView({ block: 'nearest' });
	}

	function onkeydown(e: KeyboardEvent) {
		const t = e.target as HTMLElement;
		if (t.tagName === 'INPUT' || t.tagName === 'SELECT' || t.tagName === 'TEXTAREA' || e.metaKey || e.ctrlKey || e.altKey) return;
		const hit = focused >= 0 ? data.hits[focused] : undefined;
		switch (e.key) {
			case 'j':
				move(1);
				break;
			case 'k':
				move(-1);
				break;
			case 'o':
			case 'Enter':
				if (hit) goto(`/work/${hit.id}${data.filters.q ? `?q=${encodeURIComponent(data.filters.q)}` : ''}`);
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
		{#if anyFilter}
			<a class="clear-all" href={hrefWith(url, Object.fromEntries([...Object.keys(data.filters.facets), 'from', 'to'].map((k) => [k, null])))}
				>× clear filters</a
			>
		{/if}
		<YearHistogram years={data.years} from={data.filters.yearFrom} to={data.filters.yearTo} {url} />
		{#each data.facets as f (f.key)}
			<FacetGroup facet={f} {url} limit={facetLimits[f.key] ?? 8} />
		{/each}
	</aside>

	<section class="results">
		<div class="toolbar">
			<div class="count">
				<strong>{data.total}</strong>
				{data.total === 1 ? 'work' : 'works'}
				{#if data.filters.q}matching <q>{data.filters.q}</q>{/if}
			</div>
			<div class="controls">
				<label class="sort">
					<span class="muted">Sort</span>
					<select value={sortParam} onchange={(e) => setSort(e.currentTarget.value)}>
						{#each SORTS as s (s.value)}
							<option value={s.value}>{s.value === 'auto' ? `Best (${autoLabel})` : s.label}</option>
						{/each}
					</select>
				</label>
				{#if data.sort === 'random'}
					<a class="btn" href={hrefWith(url, { seed: data.filters.seed + 1 })} title="Shuffle again">⟳ shuffle</a>
				{/if}
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
						<a href="/work/{s.id}">{s.title ?? `#${s.id}`}</a>
						<a class="x" href={hrefWith(url, { like: like.filter((x) => x !== s.id).join(',') || null })} title="Remove">×</a>
					</span>
				{/each}
				<a class="clear" href={hrefWith(url, { like: null, sort: null })}>clear</a>
				<span class="faint">· TF-IDF cosine over titles, abstracts, summaries and topics</span>
			</div>
		{/if}

		{#if data.hits.length === 0}
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
				{#each data.hits as hit, i (hit.id)}
					<WorkCard {hit} {url} {view} focused={i === focused} {like} sort={data.sort} />
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
	.layout {
		display: grid;
		grid-template-columns: 250px minmax(0, 1fr);
		gap: 28px;
		max-width: 1320px;
		margin: 0 auto;
		padding: 18px 22px 40px;
	}
	aside {
		position: sticky;
		top: 70px;
		align-self: start;
		max-height: calc(100vh - 88px);
		overflow-y: auto;
		padding-right: 6px;
	}
	.clear-all {
		display: inline-block;
		font-size: 12px;
		margin-bottom: 12px;
	}
	.toolbar {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
		padding: 0 16px 10px;
		border-bottom: 1px solid var(--border);
	}
	.count {
		color: var(--muted);
	}
	.count strong {
		color: var(--text-strong);
	}
	.count q {
		color: var(--text);
	}
	.controls {
		display: flex;
		align-items: center;
		gap: 12px;
	}
	.sort {
		display: flex;
		align-items: center;
		gap: 6px;
		font-size: 12.5px;
	}
	.sort select {
		padding: 3px 8px;
		font-size: 12.5px;
	}
	.seg {
		display: flex;
	}
	.seg .btn {
		border-radius: 0;
	}
	.seg .btn:first-child {
		border-radius: 6px 0 0 6px;
	}
	.seg .btn:last-child {
		border-radius: 0 6px 6px 0;
	}
	.seg .btn + .btn {
		margin-left: -1px;
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
