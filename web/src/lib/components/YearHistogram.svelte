<script lang="ts">
	import { collapsed } from '#lib/collapse.svelte.ts';
	import { hrefWith } from '#lib/util.ts';

	let {
		years,
		range,
		from,
		to,
		url
	}: {
		years: { year: number; count: number }[];
		range: { min: number; max: number } | null;
		from: number | null;
		to: number | null;
		url: { pathname: string; search: string };
	} = $props();

	// The axis spans the whole library (not the current results), so bars stay put.
	const lo = $derived(range?.min ?? 0);
	const hi = $derived(range?.max ?? 0);
	const counts = $derived(new Map(years.map((y) => [y.year, y.count])));
	// Bucket into at most ~24 bars.
	const step = $derived(Math.max(1, Math.ceil((hi - lo + 1) / 24)));
	const buckets = $derived.by(() => {
		const out: { start: number; end: number; count: number }[] = [];
		for (let y = lo; y <= hi; y += step) {
			let c = 0;
			for (let k = y; k < y + step; k++) c += counts.get(k) ?? 0;
			out.push({ start: y, end: Math.min(y + step - 1, hi), count: c });
		}
		return out;
	});
	const bmax = $derived(Math.max(1, ...buckets.map((b) => b.count)));
	const active = $derived(from != null || to != null);
	const isCollapsed = $derived(collapsed.is('year'));
	const inRange = (b: { start: number; end: number }) =>
		(from == null || b.end >= from) && (to == null || b.start <= to);
</script>

{#if range && range.max > range.min}
	<section class="years">
		<header>
			<button
				type="button"
				class="title"
				class:collapsed={isCollapsed}
				aria-expanded={!isCollapsed}
				onclick={() => collapsed.toggle('year')}
				title={isCollapsed ? 'Show Year' : 'Hide Year'}>Year</button
			>
			<a class="clear" class:hidden={!active} href={hrefWith(url, { from: null, to: null })} tabindex={active ? 0 : -1}>clear</a>
		</header>
		{#if !isCollapsed}
			<div class="bars">
				{#each buckets as b (b.start)}
					<a
						class="bar"
						class:dim={!inRange(b)}
						href={hrefWith(url, { from: b.start, to: b.end })}
						title="{b.start === b.end ? b.start : `${b.start}–${b.end}`}: {b.count}"
					>
						<span style:height="{b.count ? Math.max(8, (b.count / bmax) * 100) : 0}%"></span>
					</a>
				{/each}
			</div>
			<div class="axis">
				<span>{lo}</span>
				<span class="sel">{#if active}{from ?? lo}–{to ?? hi}{/if}</span>
				<span>{hi}</span>
			</div>
		{/if}
	</section>
{/if}

<style>
	.years {
		margin-bottom: 12px;
		padding: 8px 14px 8px;
		background: color-mix(in srgb, var(--bg-raised) 70%, transparent);
		border: 1px solid color-mix(in srgb, var(--border) 75%, transparent);
		border-radius: var(--radius);
	}
	header {
		display: flex;
		justify-content: space-between;
		align-items: baseline;
		height: 20px;
		margin-bottom: 6px;
	}
	.title {
		background: none;
		border: 0;
		padding: 0;
		font-size: 11px;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.07em;
		color: var(--muted);
		cursor: pointer;
	}
	.title:hover {
		color: var(--text);
	}
	.title.collapsed {
		text-decoration: line-through;
		color: var(--faint);
	}
	.clear {
		font-size: 11px;
	}
	.clear.hidden {
		visibility: hidden;
	}
	.bars {
		display: flex;
		align-items: flex-end;
		gap: 2px;
		height: 44px;
	}
	.bar {
		flex: 1;
		height: 100%;
		display: flex;
		align-items: flex-end;
	}
	.bar span {
		display: block;
		width: 100%;
		background: var(--accent);
		opacity: 0.75;
		border-radius: 2px 2px 0 0;
	}
	.bar:hover span {
		opacity: 1;
	}
	.bar.dim span {
		opacity: 0.18;
	}
	.axis {
		display: flex;
		justify-content: space-between;
		height: 16px;
		font-family: var(--mono);
		font-size: 10.5px;
		color: var(--faint);
		margin-top: 3px;
	}
	.sel {
		color: var(--accent);
	}
</style>
