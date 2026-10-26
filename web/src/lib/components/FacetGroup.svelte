<script lang="ts">
	import type { Facet } from '#lib/types.ts';
	import { hrefToggle, hrefWith } from '#lib/util.ts';

	let { facet, url, limit = 8 }: { facet: Facet; url: { pathname: string; search: string }; limit?: number } =
		$props();

	let showAll = $state(false);
	const anySelected = $derived(facet.values.some((v) => v.selected));
	const shown = $derived(showAll ? facet.values : facet.values.slice(0, limit));
	const max = $derived(Math.max(1, ...facet.values.map((v) => v.count)));
</script>

{#if facet.values.length}
	<section class="facet">
		<header>
			<h3>{facet.label}</h3>
			{#if anySelected}
				<a class="clear" href={hrefWith(url, { [facet.key]: null })}>clear</a>
			{/if}
		</header>
		<ul>
			{#each shown as v (v.value)}
				<li>
					<a class="row" class:selected={v.selected} class:empty={v.count === 0} href={hrefToggle(url, facet.key, v.value)} title={v.hint ? `${v.label} (${v.hint})` : v.label}>
						<span class="bar" style:width="{(v.count / max) * 100}%"></span>
						<span class="check" aria-hidden="true">{v.selected ? '✓' : ''}</span>
						<span class="label">{v.label}</span>
						<span class="count">{v.count}</span>
					</a>
				</li>
			{/each}
		</ul>
		{#if facet.values.length > limit}
			<button type="button" class="more" onclick={() => (showAll = !showAll)}>
				{showAll ? 'fewer' : `${facet.values.length - limit} more`}
			</button>
		{/if}
	</section>
{/if}

<style>
	.facet {
		margin-bottom: 18px;
	}
	header {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		margin-bottom: 4px;
	}
	h3 {
		margin: 0;
		font-size: 11px;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.07em;
		color: var(--muted);
	}
	.clear {
		font-size: 11px;
	}
	ul {
		list-style: none;
		margin: 0;
		padding: 0;
	}
	.row {
		position: relative;
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 2px 6px;
		border-radius: 4px;
		color: var(--text);
		font-size: 12.5px;
		overflow: hidden;
	}
	.row:hover {
		background: var(--panel);
		text-decoration: none;
	}
	.row.selected {
		color: var(--accent);
		background: var(--accent-dim);
	}
	.row.empty {
		color: var(--faint);
	}
	.bar {
		position: absolute;
		left: 0;
		top: 0;
		bottom: 0;
		background: color-mix(in srgb, var(--accent) 7%, transparent);
		border-radius: 4px;
		pointer-events: none;
	}
	.check,
	.label,
	.count {
		position: relative;
	}
	.check {
		width: 10px;
		font-size: 10px;
		flex: none;
	}
	.label {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.count {
		font-family: var(--mono);
		font-size: 11px;
		color: var(--muted);
	}
	.more {
		background: none;
		border: 0;
		color: var(--muted);
		font-size: 11.5px;
		padding: 2px 6px;
	}
	.more:hover {
		color: var(--accent);
	}
</style>
