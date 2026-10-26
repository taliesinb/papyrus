<script lang="ts">
	import { hrefWith } from '#lib/util.ts';

	let {
		years,
		from,
		to,
		url
	}: {
		years: { year: number; count: number }[];
		from: number | null;
		to: number | null;
		url: { pathname: string; search: string };
	} = $props();

	const lo = $derived(years.length ? years[0].year : 0);
	const hi = $derived(years.length ? years[years.length - 1].year : 0);
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
	const inRange = (b: { start: number; end: number }) =>
		(from == null || b.end >= from) && (to == null || b.start <= to);
</script>

{#if years.length > 1}
	<section class="years">
		<header>
			<h3>Year</h3>
			{#if from != null || to != null}
				<a class="clear" href={hrefWith(url, { from: null, to: null })}>clear</a>
			{/if}
		</header>
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
			{#if from != null || to != null}<span class="sel">{from ?? lo}–{to ?? hi}</span>{/if}
			<span>{hi}</span>
		</div>
	</section>
{/if}

<style>
	.years {
		margin-bottom: 18px;
	}
	header {
		display: flex;
		justify-content: space-between;
		align-items: baseline;
		margin-bottom: 6px;
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
		font-family: var(--mono);
		font-size: 10.5px;
		color: var(--faint);
		margin-top: 3px;
	}
	.sel {
		color: var(--accent);
	}
</style>
