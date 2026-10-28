<script lang="ts">
	import { idLabel, idLink } from '#lib/util.ts';
	// `short`: the scheme only (arXiv, DOI); the value is in the tooltip.
	let { scheme, value, short = false }: { scheme: string; value: string; short?: boolean } = $props();
	const href = $derived(idLink(scheme, value));
</script>

{#if href}
	<a class="chip" {href} target="_blank" rel="noreferrer" title="{idLabel(scheme)} {value}">
		<span class="s">{idLabel(scheme)}</span>{#if !short}<span class="v">{value}</span>{/if}
	</a>
{:else}
	<span class="chip" title="{idLabel(scheme)} {value}"><span class="s">{idLabel(scheme)}</span>{#if !short}<span class="v">{value}</span>{/if}</span>
{/if}

<style>
	.chip {
		display: inline-flex;
		align-items: stretch;
		border: 1px solid var(--border);
		border-radius: 5px;
		overflow: hidden;
		font-size: 11.5px;
		line-height: 18px;
		max-width: 100%;
		color: var(--text);
	}
	a.chip:hover {
		border-color: var(--accent);
		text-decoration: none;
	}
	.s {
		background: var(--panel-2);
		color: var(--muted);
		padding: 0 5px;
		font-weight: 600;
		white-space: nowrap;
	}
	.v {
		font-family: var(--mono);
		font-size: 11px;
		padding: 0 6px;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
</style>
