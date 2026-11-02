<script lang="ts">
	import type { AssertionRow } from '#lib/types.ts';
	import { formatDate } from '#lib/util.ts';

	let { assertions }: { assertions: AssertionRow[] } = $props();

	let showSuperseded = $state(false);
	const rows = $derived(showSuperseded ? assertions : assertions.filter((a) => !a.superseded));
	const groups = $derived.by(() => {
		const m = new Map<string, AssertionRow[]>();
		for (const a of rows) {
			const l = m.get(a.field) ?? [];
			l.push(a);
			m.set(a.field, l);
		}
		return [...m];
	});
	const supersededCount = $derived(assertions.filter((a) => a.superseded).length);

	function pretty(raw: string): { text: string; long: boolean } {
		let v: unknown = raw;
		try {
			v = JSON.parse(raw);
		} catch {
			/* not JSON */
		}
		let text: string;
		if (typeof v === 'string') text = v;
		else if (Array.isArray(v) && v.every((x) => typeof x === 'string')) text = v.join(' · ');
		else if (Array.isArray(v) && v.every((x) => x && typeof x === 'object' && 'name' in x))
			text = v.map((x) => (x as { name: string }).name).join(' · ');
		else if (v && typeof v === 'object' && !Array.isArray(v) && Object.values(v).every((x) => typeof x === 'number'))
			text = Object.entries(v as Record<string, number>)
				.sort((a, b) => b[1] - a[1])
				.map(([k, p]) => `${k} ${(p * 100).toFixed(0)}%`)
				.join(' · ');
		else text = JSON.stringify(v);
		return { text, long: text.length > 220 };
	}

	function sourceClass(s: string): string {
		if (s.startsWith('llm:') || s === 'extract') return 'llm';
		if (s.startsWith('classify:') || s.startsWith('screen:')) return 'classify';
		if (s.startsWith('api:')) return 'api';
		if (s.startsWith('heuristic:') || s.startsWith('pdf:')) return 'heur';
		if (s === 'user') return 'user';
		return '';
	}
</script>

<div class="prov">
	<div class="bar">
		<span class="muted">{rows.length} claims from {new Set(rows.map((r) => r.source)).size} sources</span>
		{#if supersededCount}
			<label><input type="checkbox" bind:checked={showSuperseded} /> show {supersededCount} superseded</label>
		{/if}
	</div>
	{#each groups as [field, items] (field)}
		<div class="field">
			<div class="fname">{field.replaceAll('_', ' ')}</div>
			<ul>
				{#each items as a (a.id)}
					{@const p = pretty(a.value)}
					<li class:superseded={a.superseded}>
						<div class="src">
							<span class="source {sourceClass(a.source)}" title={a.source}>{a.source.replace(/^(llm|classify):lmstudio:/, '$1:')}</span>
							{#if a.subjectType === 'file'}<span class="subj" title="claim about file #{a.subjectId}">file</span>{/if}
							{#if a.confidence != null && a.confidence < 1}<span class="conf">{(a.confidence * 100).toFixed(0)}%</span>{/if}
							<span class="when">{formatDate(a.createdAt)}</span>
						</div>
						{#if p.long}
							<details><summary>{p.text.slice(0, 200)}…</summary><div class="full">{p.text}</div></details>
						{:else}
							<div class="val">{p.text}</div>
						{/if}
					</li>
				{/each}
			</ul>
		</div>
	{/each}
</div>

<style>
	.bar {
		display: flex;
		justify-content: space-between;
		font-size: 12px;
		margin-bottom: 8px;
	}
	.bar label {
		color: var(--muted);
		display: flex;
		gap: 5px;
		align-items: center;
	}
	.field {
		display: grid;
		grid-template-columns: 110px 1fr;
		gap: 10px;
		padding: 6px 0;
		border-top: 1px solid var(--border);
	}
	.fname {
		font-size: 11.5px;
		color: var(--muted);
		text-transform: capitalize;
		padding-top: 2px;
	}
	ul {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 6px;
		min-width: 0;
	}
	li.superseded {
		opacity: 0.45;
	}
	li.superseded .val {
		text-decoration: line-through;
	}
	.src {
		display: flex;
		gap: 6px;
		align-items: center;
		font-size: 10.5px;
	}
	.source {
		font-family: var(--mono);
		padding: 0 5px;
		border-radius: 3px;
		background: var(--panel-2);
		color: var(--muted);
	}
	.source.llm {
		color: var(--accent-2);
	}
	.source.classify {
		color: #f39ac6;
	}
	.source.api {
		color: var(--good);
	}
	.source.heur {
		color: var(--warn);
	}
	.source.user {
		color: var(--accent);
	}
	.subj,
	.conf,
	.when {
		color: var(--faint);
	}
	.val,
	details {
		font-size: 12.5px;
		color: var(--text);
		overflow-wrap: anywhere;
		margin-top: 1px;
	}
	summary {
		cursor: pointer;
		color: var(--text);
	}
	.full {
		margin-top: 4px;
		color: #b4bcc8;
	}
</style>
