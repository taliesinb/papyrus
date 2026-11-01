<script lang="ts">
	// The pipeline's state, polled from /api/version every 8 s: a pipeline run in
	// progress (classify, match…) and, once it has written something, a button that
	// reloads the page's data. The first status comes with the root layout's data.
	import { refreshAll } from '$app/navigation';
	import { page } from '$app/state';
	import { onMount } from 'svelte';
	import { href } from '#lib/paths.ts';
	import type { PipelineStatus } from '#lib/types.ts';
	import { relativeTime } from '#lib/util.ts';

	let live = $state<PipelineStatus | null>(null);
	const loaded = $derived(page.data.status as PipelineStatus);
	const status = $derived(live ?? loaded);
	const changed = $derived(live != null && live.version !== loaded.version);

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
</script>

{#each status.running as r (r.id)}
	<span class="running" title="{r.command} {r.backend ?? ''} · run {r.id}, started {relativeTime(r.startedAt)}"
		><span class="pulse"></span>{r.command}</span
	>
{/each}
{#if changed}
	<button class="btn small active" type="button" onclick={refresh} title="The pipeline wrote to the database">refresh</button>
{/if}

<style>
	.running {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		font-size: 12px;
		color: var(--warn);
		white-space: nowrap;
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
	.btn.small {
		font-size: 11.5px;
		padding: 1px 8px;
	}
</style>
