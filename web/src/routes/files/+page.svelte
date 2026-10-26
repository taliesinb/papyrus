<script lang="ts">
	import KindBadge from '#lib/components/KindBadge.svelte';
	import Thumb from '#lib/components/Thumb.svelte';
	import { formatDate, formatSize } from '#lib/util.ts';
	import type { PageProps } from './$types';

	let { data }: PageProps = $props();

	type Show = 'all' | 'unlinked' | 'linked' | 'missing';
	let show = $state<Show>('all');
	let filter = $state('');
	let sortKey = $state<'added' | 'name' | 'size' | 'pages'>('added');

	const rows = $derived.by(() => {
		const f = filter.trim().toLowerCase();
		const out = data.files.filter((r) => {
			if (show === 'unlinked' && r.works.length) return false;
			if (show === 'linked' && !r.works.length) return false;
			if (show === 'missing' && r.available) return false;
			if (f && !`${r.filename ?? ''} ${r.folder ?? ''} ${r.works.map((w) => w.title).join(' ')}`.toLowerCase().includes(f))
				return false;
			return true;
		});
		const cmp: Record<typeof sortKey, (a: (typeof out)[number], b: (typeof out)[number]) => number> = {
			added: (a, b) => (b.addedAt ?? '').localeCompare(a.addedAt ?? ''),
			name: (a, b) => (a.filename ?? '').localeCompare(b.filename ?? ''),
			size: (a, b) => b.size - a.size,
			pages: (a, b) => (b.pageCount ?? 0) - (a.pageCount ?? 0)
		};
		return out.sort(cmp[sortKey]);
	});
	const counts = $derived({
		all: data.files.length,
		linked: data.files.filter((r) => r.works.length).length,
		unlinked: data.files.filter((r) => !r.works.length).length,
		missing: data.files.filter((r) => !r.available).length
	});
</script>

<svelte:head><title>Files · papyrus</title></svelte:head>

<div class="page">
	<div class="head">
		<h1>Files</h1>
		<p class="muted">
			Every PDF the scanner has seen, including ones triage decided are not library material. A file can appear at several
			paths; works link to files.
		</p>
	</div>
	<div class="toolbar">
		<div class="seg">
			{#each ['all', 'linked', 'unlinked', 'missing'] as const as s (s)}
				<button type="button" class="btn" class:active={show === s} onclick={() => (show = s)}>{s} <span class="muted">{counts[s]}</span></button>
			{/each}
		</div>
		<input type="search" placeholder="Filter by name or folder…" bind:value={filter} />
		<label class="muted"
			>Sort <select bind:value={sortKey}>
				<option value="added">Recently added</option>
				<option value="name">Name</option>
				<option value="size">Size</option>
				<option value="pages">Pages</option>
			</select></label
		>
	</div>
	<table>
		<thead>
			<tr>
				<th></th>
				<th>File</th>
				<th>Triage says</th>
				<th class="num">Pages</th>
				<th class="num">Size</th>
				<th>Text</th>
				<th>Added</th>
				<th>Work</th>
			</tr>
		</thead>
		<tbody>
			{#each rows as r (r.id)}
				<tr class:gone={!r.available}>
					<td class="th"><Thumb fileId={r.available ? r.id : null} width={160} /></td>
					<td class="name">
						{#if r.available}
							<a href="/api/files/{r.id}/pdf" target="_blank" rel="noreferrer">{r.filename ?? `#${r.id}`}</a>
						{:else}
							{r.filename ?? `#${r.id}`}
						{/if}
						<div class="faint">{r.folder}{r.paths > 1 ? ` · ${r.paths} paths` : ''}{!r.available ? ' · not on disk' : ''}</div>
					</td>
					<td>
						{#if r.kind}<KindBadge kind={r.kind} small />{/if}
						{#if r.library === false}<span class="notlib">not library</span>{/if}
					</td>
					<td class="num">{r.pageCount ?? ''}</td>
					<td class="num">{formatSize(r.size)}</td>
					<td><span class="ts {r.textStatus}">{r.textStatus.replace('_', ' ')}</span></td>
					<td class="date">{formatDate(r.addedAt)}</td>
					<td class="works">
						{#each r.works as w (w.id)}
							<a href="/work/{w.id}" class:rejected={w.status === 'rejected'}>{w.title ?? `#${w.id}`}</a>
						{:else}
							<span class="faint">—</span>
						{/each}
					</td>
				</tr>
			{/each}
		</tbody>
	</table>
</div>

<style>
	.page {
		max-width: 1320px;
		margin: 0 auto;
		padding: 20px 22px 60px;
	}
	h1 {
		font-family: var(--serif);
		font-size: 24px;
		margin: 0;
		color: var(--text-strong);
	}
	.head p {
		margin: 4px 0 16px;
		font-size: 13px;
	}
	.toolbar {
		display: flex;
		gap: 12px;
		align-items: center;
		margin-bottom: 12px;
	}
	.toolbar input {
		width: 280px;
	}
	.seg {
		display: flex;
	}
	.seg .btn {
		border-radius: 0;
		text-transform: capitalize;
	}
	.seg .btn:first-child {
		border-radius: 6px 0 0 6px;
	}
	.seg .btn:last-child {
		border-radius: 0 6px 6px 0;
	}
	.seg .btn + .btn {
		margin-left: -1px;
	}
	table {
		width: 100%;
		border-collapse: collapse;
		font-size: 12.5px;
	}
	th {
		text-align: left;
		font-size: 11px;
		text-transform: uppercase;
		letter-spacing: 0.06em;
		color: var(--muted);
		font-weight: 600;
		padding: 6px 8px;
		border-bottom: 1px solid var(--border);
	}
	td {
		padding: 6px 8px;
		border-bottom: 1px solid var(--border);
		vertical-align: top;
	}
	tr:hover td {
		background: var(--bg-raised);
	}
	tr.gone {
		opacity: 0.5;
	}
	.th {
		width: 40px;
	}
	.th :global(.thumb) {
		width: 32px;
	}
	.name {
		max-width: 380px;
		overflow-wrap: anywhere;
	}
	.name a {
		color: var(--text-strong);
	}
	.num {
		text-align: right;
		font-family: var(--mono);
		font-size: 11.5px;
		white-space: nowrap;
	}
	.date {
		white-space: nowrap;
		color: var(--muted);
	}
	.notlib {
		display: inline-block;
		font-size: 10.5px;
		color: var(--bad);
		margin-left: 4px;
	}
	.ts {
		font-size: 11.5px;
		color: var(--muted);
	}
	.ts.no_text {
		color: var(--warn);
	}
	.ts.error {
		color: var(--bad);
	}
	.works {
		max-width: 360px;
	}
	.works a {
		display: block;
		font-family: var(--serif);
		font-size: 13.5px;
		color: var(--text);
	}
	.works a.rejected {
		color: var(--faint);
		text-decoration: line-through;
	}
</style>
