<script lang="ts">
	import { collapsed } from '#lib/collapse.svelte.ts';
	import { facetFocus } from '#lib/facetFocus.svelte.ts';
	import type { Facet } from '#lib/types.ts';
	import { hrefToggle, hrefWith } from '#lib/util.ts';

	let { facet, url, limit = 8 }: { facet: Facet; url: { pathname: string; search: string }; limit?: number } =
		$props();

	// Rows have a fixed height and the server keeps the order fixed, so a
	// pane's height depends only on which values are visible.
	const ROW = 24;

	let showAll = $state(false);
	const isCollapsed = $derived(collapsed.is(facet.key));
	const anySelected = $derived(facet.values.some((v) => v.selected));

	// Visible values: zero-count rows are hidden (they only appear when another
	// pane, the year range or a search has narrowed the set), except selected
	// ones. While the user is clicking in some pane, every pane keeps the rows
	// it showed before the first click (see facetFocus), so nothing moves.
	const visible = $derived.by(() => {
		const frozen = facetFocus.frozen?.[facet.key];
		if (frozen) {
			const keep = new Set(frozen);
			return facet.values.filter((v) => keep.has(v.value) || v.selected);
		}
		return facet.values.filter((v) => v.count > 0 || v.selected);
	});
	$effect(() => {
		facetFocus.visible[facet.key] = visible.map((v) => v.value);
		return () => delete facetFocus.visible[facet.key];
	});

	// Selected values beyond the limit are still shown, after the top ones.
	const shown = $derived(
		showAll ? visible : [...visible.slice(0, limit), ...visible.slice(limit).filter((v) => v.selected)]
	);
	const max = $derived(Math.max(1, ...facet.values.map((v) => v.count)));

	let lastPointer = 'mouse';
	function onclick(e: MouseEvent) {
		const a = (e.target as HTMLElement).closest('a.row, a.clear');
		if (!a) return;
		const pt = (e as PointerEvent).pointerType || (e.detail === 0 ? 'keyboard' : lastPointer);
		facetFocus.pin(facet.key, pt);
	}
</script>

{#if facet.values.length}
	<!-- Delegated: clicks come from the (keyboard-accessible) links inside. -->
	<!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
	<section
		class="facet"
		{onclick}
		onpointerdown={(e) => (lastPointer = e.pointerType)}
		onpointerleave={(e) => e.pointerType !== 'touch' && facetFocus.leave(facet.key)}
	>
		<header>
			<button
				type="button"
				class="title"
				class:collapsed={isCollapsed}
				aria-expanded={!isCollapsed}
				onclick={() => collapsed.toggle(facet.key)}
				title={isCollapsed ? `Show ${facet.label}` : `Hide ${facet.label}`}>{facet.label}</button
			>
			<a class="clear" class:hidden={!anySelected} href={hrefWith(url, { [facet.key]: null })} tabindex={anySelected ? 0 : -1}
				>clear</a
			>
		</header>
		{#if !isCollapsed}
			<ul style:--row="{ROW}px">
				{#each shown as v (v.value)}
					<li>
						<a
							class="row"
							class:selected={v.selected}
							class:empty={v.count === 0}
							href={hrefToggle(url, facet.key, v.value)}
							title={v.hint ? `${v.label} (${v.hint})` : v.label}
						>
							<span class="bar" style:width="{(v.count / max) * 100}%"></span>
							<span class="check" aria-hidden="true">{v.selected ? '✓' : ''}</span>
							<span class="label">{v.label}</span>
							<span class="count">{v.count}</span>
						</a>
					</li>
				{:else}
					<li class="none">none in these results</li>
				{/each}
			</ul>
			{#if visible.length > limit}
				<button type="button" class="more" onclick={() => (showAll = !showAll)}>
					{showAll ? 'fewer' : `${visible.length - limit} more`}
				</button>
			{/if}
		{/if}
	</section>
{/if}

<style>
	.facet {
		margin-bottom: 12px;
		padding: 8px 8px 6px;
		background: color-mix(in srgb, var(--bg-raised) 70%, transparent);
		border: 1px solid color-mix(in srgb, var(--border) 75%, transparent);
		border-radius: var(--radius);
	}
	header {
		padding: 0 6px;
	}
	header {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		height: 20px;
		margin-bottom: 4px;
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
		height: var(--row);
		padding: 0 6px;
		border-radius: 4px;
		color: var(--text);
		font-size: 12.5px;
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
	.row.empty .count {
		color: var(--faint);
	}
	.bar {
		position: absolute;
		/* Square-ended, inset 1px top and bottom so neighbouring bars stay separate.
		   (The row no longer clips with overflow: hidden, which rounded its corners.) */
		left: 0;
		top: 1px;
		bottom: 1px;
		max-width: 100%;
		background: color-mix(in srgb, var(--accent) 7%, transparent);
		border-radius: 0;
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
	.none {
		height: var(--row);
		line-height: var(--row);
		padding: 0 6px;
		font-size: 12px;
		color: var(--faint);
	}
	.more {
		display: block;
		height: 22px;
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
