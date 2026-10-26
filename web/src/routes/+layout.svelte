<script lang="ts">
	import '../app.css';
	import { goto, refreshAll } from '$app/navigation';
	import { page } from '$app/state';
	import { onMount } from 'svelte';
	import type { PipelineStatus } from '#lib/types.ts';
	import { hrefWith, relativeTime } from '#lib/util.ts';
	import type { LayoutProps } from './$types';

	let { children, data }: LayoutProps = $props();

	let live = $state<PipelineStatus | null>(null);
	const status = $derived(live ?? data.status);
	const changed = $derived(live != null && live.version !== data.status.version);

	let query = $state(page.url.searchParams.get('q') ?? '');
	let searchEl: HTMLInputElement | undefined = $state();
	let timer: ReturnType<typeof setTimeout> | undefined;

	// Keep the box in sync when navigating (back/forward, clicking a chip).
	$effect(() => {
		const q = page.url.searchParams.get('q') ?? '';
		if (document.activeElement !== searchEl) query = q;
	});

	function submit(immediate = false) {
		clearTimeout(timer);
		const run = () => {
			const onLibrary = page.url.pathname === '/';
			const target = onLibrary
				? hrefWith(page.url, { q: query.trim() || null })
				: hrefWith({ pathname: '/', search: '' }, { q: query.trim() || null });
			goto(target, { replace: onLibrary, reset: !onLibrary });
		};
		if (immediate) run();
		else timer = setTimeout(run, 220);
	}

	onMount(() => {
		const poll = async () => {
			try {
				const r = await fetch('/api/version');
				if (r.ok) live = await r.json();
			} catch {
				/* server restarting */
			}
		};
		const id = setInterval(poll, 8000);
		return () => clearInterval(id);
	});

	async function refresh() {
		await refreshAll();
		live = null;
	}

	function onkeydown(e: KeyboardEvent) {
		const t = e.target as HTMLElement;
		const typing = t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable;
		if (e.key === '/' && !typing) {
			e.preventDefault();
			searchEl?.focus();
			searchEl?.select();
		} else if (e.key === 'Escape' && t === searchEl) {
			searchEl?.blur();
		}
	}
</script>

<svelte:window {onkeydown} />

<svelte:head>
	<title>papyrus</title>
	<link
		rel="icon"
		href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>📜</text></svg>"
	/>
</svelte:head>

<header class="top">
	<a class="brand" href="/">papyrus</a>
	<nav>
		<a href="/" class:current={page.url.pathname === '/'}>Library</a>
		<a href="/files" class:current={page.url.pathname === '/files'}>Files</a>
	</nav>
	<form class="search" role="search" onsubmit={(e) => (e.preventDefault(), submit(true))}>
		<input
			bind:this={searchEl}
			type="search"
			placeholder="Search titles, authors, abstracts and page text…"
			bind:value={query}
			oninput={() => submit()}
			aria-label="Search"
		/>
		<kbd class="hint">/</kbd>
	</form>
	<div class="status">
		{#if changed}
			<button class="btn active" type="button" onclick={refresh} title="The pipeline wrote to the database"
				>library updated · refresh</button
			>
		{/if}
		{#each status.running as r (r.id)}
			<span class="running" title="{r.command} {r.backend ?? ''} · run {r.id}"
				><span class="pulse"></span>{r.command} running · {relativeTime(r.startedAt)}</span
			>
		{/each}
		<span
			class="counts"
			title="{status.counts.files} files · {status.counts.people} people · {status.counts.pages} pages of text indexed"
		>
			{status.counts.works} works · {status.counts.files} files
		</span>
	</div>
</header>

<main>
	{@render children()}
</main>

<style>
	.top {
		position: sticky;
		top: 0;
		z-index: 20;
		display: flex;
		align-items: center;
		gap: 20px;
		height: 52px;
		padding: 0 18px;
		background: color-mix(in srgb, var(--bg) 88%, transparent);
		backdrop-filter: blur(10px);
		border-bottom: 1px solid var(--border);
	}
	.brand {
		font-family: var(--serif);
		font-size: 20px;
		font-weight: 700;
		font-style: italic;
		color: var(--text-strong);
		letter-spacing: 0.01em;
	}
	.brand:hover {
		text-decoration: none;
		color: var(--accent);
	}
	nav {
		display: flex;
		gap: 4px;
	}
	nav a {
		color: var(--muted);
		padding: 4px 10px;
		border-radius: 6px;
		font-size: 13px;
	}
	nav a:hover {
		color: var(--text);
		text-decoration: none;
		background: var(--panel);
	}
	nav a.current {
		color: var(--text-strong);
		background: var(--panel);
	}
	.search {
		position: relative;
		flex: 1;
		max-width: 640px;
	}
	.search input {
		width: 100%;
		padding: 7px 34px 7px 12px;
		background: var(--panel);
	}
	.hint {
		position: absolute;
		right: 10px;
		top: 50%;
		transform: translateY(-50%);
		pointer-events: none;
	}
	.status {
		margin-left: auto;
		display: flex;
		align-items: center;
		gap: 12px;
		font-size: 12px;
		color: var(--muted);
		white-space: nowrap;
	}
	.running {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		color: var(--warn);
	}
	.pulse {
		width: 7px;
		height: 7px;
		border-radius: 50%;
		background: var(--warn);
		animation: pulse 1.6s ease-in-out infinite;
	}
	@keyframes pulse {
		50% {
			opacity: 0.25;
		}
	}
	main {
		min-height: calc(100vh - 52px);
	}
</style>
