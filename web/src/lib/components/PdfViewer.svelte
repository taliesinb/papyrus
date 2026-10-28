<script lang="ts">
	// The browser's own PDF viewer in an iframe, plus an arxiv-sanity style
	// strip of page thumbnails. Jumping to a page uses the #page= fragment.
	import { onMount } from 'svelte';
	import { href } from '#lib/paths.ts';
	import Thumb from './Thumb.svelte';

	let {
		fileId,
		pageCount,
		page = $bindable(1),
		available = true
	}: { fileId: number; pageCount: number | null; page?: number; available?: boolean } = $props();

	const STRIP_MAX = 30;
	let darkPages = $state(false);
	let showStrip = $state(true);
	// Bumped to force the iframe to reload at the new page.
	let nonce = $state(0);

	onMount(() => {
		darkPages = localStorage.getItem('papyrus.darkPages') === '1';
		showStrip = localStorage.getItem('papyrus.strip') !== '0';
	});
	$effect(() => localStorage.setItem('papyrus.darkPages', darkPages ? '1' : '0'));
	$effect(() => localStorage.setItem('papyrus.strip', showStrip ? '1' : '0'));

	const total = $derived(pageCount ?? 1);
	const stripPages = $derived(Array.from({ length: Math.min(total, STRIP_MAX) }, (_, i) => i + 1));
	const src = $derived(href(`/api/files/${fileId}/pdf#page=${page}&navpanes=0&view=FitH`));

	function go(p: number) {
		page = Math.max(1, Math.min(total, p));
		nonce++;
	}

	let stripEl: HTMLDivElement | undefined = $state();
	$effect(() => {
		void page;
		stripEl?.querySelector(`[data-page="${page}"]`)?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
	});
</script>

<div class="viewer">
	<div class="toolbar">
		<button class="btn" type="button" onclick={() => go(page - 1)} disabled={page <= 1} title="Previous page">‹</button>
		<label class="pg">
			<input
				type="number"
				min="1"
				max={total}
				value={page}
				onchange={(e) => go(Number(e.currentTarget.value) || 1)}
				aria-label="Page"
			/>
			<span class="muted">/ {pageCount ?? '?'}</span>
		</label>
		<button class="btn" type="button" onclick={() => go(page + 1)} disabled={page >= total} title="Next page">›</button>
		<span class="sep"></span>
		<button class="btn" class:active={showStrip} type="button" onclick={() => (showStrip = !showStrip)} title="Page thumbnails">Pages</button>
		<button
			class="btn"
			class:active={darkPages}
			type="button"
			onclick={() => (darkPages = !darkPages)}
			title="Invert page colours for reading in the dark">◐ Dark pages</button
		>
		<span class="spacer"></span>
		<a class="btn" href={href(`/api/files/${fileId}/pdf#page=${page}`)} target="_blank" rel="noreferrer">Open ↗</a>
		<a class="btn" href={href(`/api/files/${fileId}/pdf?download`)}>Download</a>
	</div>

	{#if showStrip && available}
		<div class="strip" bind:this={stripEl}>
			{#each stripPages as p (fileId + ':' + p)}
				<button type="button" class="tp" data-page={p} onclick={() => go(p)} title="Page {p}">
					<Thumb {fileId} page={p} width={160} active={p === page} />
					<span class="n" class:cur={p === page}>{p}</span>
				</button>
			{/each}
			{#if total > STRIP_MAX}
				<div class="rest muted">+{total - STRIP_MAX} pages</div>
			{/if}
		</div>
	{/if}

	<div class="frame">
		{#if available}
			{#key `${fileId}:${nonce}`}
				<iframe title="PDF" {src} class:dark={darkPages}></iframe>
			{/key}
		{:else}
			<div class="missing">
				<p>This file is no longer at any recorded path.</p>
				<p class="muted">Re-run <code>papyrus scan</code> after moving files, so papyrus can find them again.</p>
			</div>
		{/if}
	</div>
</div>

<style>
	.viewer {
		display: flex;
		flex-direction: column;
		height: 100%;
		min-height: 0;
		background: #0a0b0d;
	}
	.toolbar {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 8px 12px;
		border-bottom: 1px solid var(--border);
		background: var(--bg-raised);
	}
	.toolbar .btn:disabled {
		opacity: 0.4;
		cursor: default;
	}
	.pg {
		display: flex;
		align-items: center;
		gap: 6px;
		font-size: 12.5px;
	}
	.pg input {
		width: 58px;
		padding: 3px 6px;
		text-align: right;
		font-size: 12.5px;
	}
	.sep {
		width: 8px;
	}
	.spacer {
		flex: 1;
	}
	.strip {
		display: flex;
		gap: 8px;
		padding: 10px 12px;
		overflow-x: auto;
		border-bottom: 1px solid var(--border);
		background: var(--bg-raised);
		flex: none;
	}
	.tp {
		flex: none;
		width: 62px;
		padding: 0;
		border: 0;
		background: none;
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 3px;
	}
	.n {
		font-family: var(--mono);
		font-size: 10px;
		color: var(--faint);
	}
	.n.cur {
		color: var(--accent);
	}
	.rest {
		align-self: center;
		font-size: 11.5px;
		white-space: nowrap;
		padding: 0 8px;
	}
	.frame {
		flex: 1;
		min-height: 0;
		position: relative;
	}
	iframe {
		position: absolute;
		inset: 0;
		width: 100%;
		height: 100%;
		border: 0;
		background: #2a2a2e;
	}
	iframe.dark {
		filter: invert(0.88) hue-rotate(180deg);
	}
	.missing {
		padding: 40px;
		color: var(--text);
	}
	.missing code {
		font-family: var(--mono);
		background: var(--panel);
		padding: 1px 5px;
		border-radius: 4px;
	}
</style>
