<script lang="ts">
	// A lazily loaded page thumbnail rendered server-side by pdftoppm.
	let {
		fileId,
		page = 1,
		width = 320,
		alt = '',
		active = false
	}: { fileId: number | null; page?: number; width?: number; alt?: string; active?: boolean } = $props();

	let failed = $state(false);
	let loaded = $state(false);
</script>

<div class="thumb" class:loaded class:active>
	{#if fileId != null && !failed}
		<img
			src="/api/files/{fileId}/thumb/{page}?w={width}"
			{alt}
			loading="lazy"
			decoding="async"
			onload={() => (loaded = true)}
			onerror={() => (failed = true)}
		/>
	{:else}
		<span class="none">no preview</span>
	{/if}
</div>

<style>
	.thumb {
		position: relative;
		aspect-ratio: 0.77;
		background: linear-gradient(110deg, #181b21 30%, #1f232b 50%, #181b21 70%);
		background-size: 200% 100%;
		animation: shimmer 1.4s linear infinite;
		border-radius: 4px;
		overflow: hidden;
		border: 1px solid var(--border);
	}
	.thumb.loaded {
		animation: none;
		background: #fff;
	}
	.thumb.active {
		outline: 2px solid var(--accent);
		outline-offset: 1px;
	}
	img {
		display: block;
		width: 100%;
		height: 100%;
		object-fit: cover;
		object-position: top;
		/* soften white pages in a dark UI */
		filter: brightness(0.92) contrast(1.02);
	}
	.none {
		position: absolute;
		inset: 0;
		display: grid;
		place-items: center;
		font-size: 10px;
		color: var(--faint);
	}
	@keyframes shimmer {
		to {
			background-position: -200% 0;
		}
	}
</style>
