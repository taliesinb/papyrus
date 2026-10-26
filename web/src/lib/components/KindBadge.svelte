<script lang="ts">
	let { kind, small = false }: { kind: string | null; small?: boolean } = $props();
	const k = $derived(kind ?? 'other');
</script>

<span class="badge" class:small style:--c="var(--kind-{k}, var(--kind-other))">{k.replace('_', ' ')}</span>

<style>
	.badge {
		display: inline-block;
		font-size: 10.5px;
		font-weight: 600;
		letter-spacing: 0.04em;
		text-transform: uppercase;
		color: var(--c);
		border: 1px solid color-mix(in srgb, var(--c) 40%, transparent);
		background: color-mix(in srgb, var(--c) 10%, transparent);
		border-radius: 4px;
		padding: 0 5px;
		line-height: 17px;
		white-space: nowrap;
		vertical-align: 2px;
	}
	.small {
		font-size: 9.5px;
		line-height: 15px;
		padding: 0 4px;
	}
</style>
