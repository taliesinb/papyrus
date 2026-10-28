<script lang="ts">
	import '../app.css';
	import { goto, refreshAll } from '$app/navigation';
	import { page } from '$app/state';
	import { onMount } from 'svelte';
	import { asset } from '$app/paths';
	import { href } from '#lib/paths.ts';
	import { installTooltips } from '#lib/tooltip.ts';
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
			const onLibrary = page.route.id === '/';
			const target = onLibrary
				? hrefWith(page.url, { q: query.trim() || null })
				: hrefWith({ pathname: href('/'), search: '' }, { q: query.trim() || null });
			goto(target, { replace: onLibrary, reset: !onLibrary });
		};
		if (immediate) run();
		else timer = setTimeout(run, 220);
	}

	onMount(installTooltips);

	// <html class="narrow"> while the window is under two thirds of the screen's width:
	// the List view then drops its cover thumbnails (WorkCard).
	onMount(() => {
		const fit = () => document.documentElement.classList.toggle('narrow', innerWidth < (screen.width * 2) / 3);
		fit();
		addEventListener('resize', fit);
		return () => removeEventListener('resize', fit);
	});

	onMount(() => {
		const poll = async () => {
			try {
				const r = await fetch(href('/api/version'));
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
	<link rel="icon" type="image/svg+xml" href={asset('favicon.svg')} />
	<link rel="apple-touch-icon" href={asset('apple-touch-icon.png')} />
</svelte:head>

<!-- Three cells: the search over the filter columns (it is one more filter), the name
     centred in the window, the pipeline status and the Library / Files switch at the right. -->
<header class="top">
	<form class="search" role="search" onsubmit={(e) => (e.preventDefault(), submit(true))}>
		<input
			bind:this={searchEl}
			type="search"
			placeholder="Search titles, authors, abstracts, text…"
			bind:value={query}
			oninput={() => submit()}
			aria-label="Search titles, authors, abstracts and the text of every page"
		/>
		<kbd class="hint">/</kbd>
	</form>
	<a class="brand" href={href('/')}>papyrus</a>
	<div class="right">
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
		<nav>
			<a href={href('/')} class:current={page.route.id === '/'}>Library</a>
			<a href={href('/files')} class:current={page.route.id === '/files'}>Files</a>
		</nav>
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
		display: grid;
		/* The sides share the spare width equally, so the name sits in the middle (unless
		   a side's content needs more, e.g. a running pipeline and "library updated"). */
		grid-template-columns: minmax(max-content, 1fr) auto minmax(max-content, 1fr);
		align-items: center;
		gap: 20px;
		height: 52px;
		padding: 0 var(--header-x);
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
	/* Exactly over the Library page's two filter columns (app.css: --page-x, --filters). */
	.search {
		position: relative;
		justify-self: start;
		width: var(--filters);
		margin-left: calc(var(--page-x) - var(--header-x));
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
	.right {
		display: flex;
		justify-content: flex-end;
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
