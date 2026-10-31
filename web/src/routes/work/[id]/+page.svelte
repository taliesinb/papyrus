<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import IdChip from '#lib/components/IdChip.svelte';
	import KindBadge from '#lib/components/KindBadge.svelte';
	import PdfViewer from '#lib/components/PdfViewer.svelte';
	import Provenance from '#lib/components/Provenance.svelte';
	import Thumb from '#lib/components/Thumb.svelte';
	import { href } from '#lib/paths.ts';
	import { authorLine, formatDate, formatSize, hostOf } from '#lib/util.ts';
	import type { PageProps } from './$types';

	let { data }: PageProps = $props();
	const w = $derived(data.work);

	// The viewer owns the page; mirror it into the URL without a navigation.
	let viewPage = $state(1);
	let viewFile = $state<number | null>(null);
	$effect.pre(() => {
		viewPage = data.page;
		viewFile = data.fileId;
	});
	$effect(() => {
		const u = new URL(page.url.href);
		if (viewPage === 1) u.searchParams.delete('page');
		else u.searchParams.set('page', String(viewPage));
		if (viewFile != null && viewFile !== w.primaryFileId) u.searchParams.set('file', String(viewFile));
		else u.searchParams.delete('file');
		if (u.href !== page.url.href) goto(u.pathname + u.search, { replace: true, reset: false, shallow: true });
	});

	const currentFile = $derived(w.files.find((f) => f.id === viewFile) ?? w.files[0]);
	const backHref = $derived(href(data.q ? `/?q=${encodeURIComponent(data.q)}` : '/'));
	const topicGroups = $derived.by(() => {
		const arxiv = w.topics.filter((t) => t.scheme === 'arxiv' && t.level === 'topic');
		const openalex = w.topics.filter((t) => t.scheme === 'openalex');
		const llm = new Map<string, string[]>();
		for (const t of w.topics.filter((t) => t.scheme === 'llm' && t.level === 'topic')) {
			const k = t.parent ?? 'other';
			llm.set(k, [...(llm.get(k) ?? []), t.name]);
		}
		return { arxiv, openalex, llm: [...llm] };
	});
	const maxTerm = $derived(Math.max(0.0001, ...data.terms.map((t) => t.weight)));

	let revealing = $state(false);
	async function reveal(fileId: number) {
		revealing = true;
		try {
			await fetch(href(`/api/files/${fileId}/reveal`), { method: 'POST' });
		} finally {
			setTimeout(() => (revealing = false), 600);
		}
	}

	let wide = $state(false);
</script>

<svelte:head>
	<title>{w.title ?? 'Untitled'} · {data.brand}</title>
</svelte:head>

<div class="work" class:wide>
	<div class="info">
		<div class="nav">
			<a href={backHref}>← Library</a>
			<span class="faint">#{w.id}</span>
		</div>

		<div class="kind">
			<KindBadge kind={w.kind} />
			<span class="status {w.status}" title="Works start as candidates until reviewed">{w.status}</span>
			{#if w.language && w.language !== 'en'}<span class="muted">{w.language}</span>{/if}
		</div>
		<h1>{w.title ?? 'Untitled'}</h1>
		{#if w.subtitle}<p class="subtitle">{w.subtitle}</p>{/if}

		{#if w.authors.length}
			<ul class="authors">
				{#each w.authors as a, i (i)}
					<li>
						<a href={href(`/?author=${a.personId != null ? `p${a.personId}` : encodeURIComponent(`n:${a.name}`)}`)} title="Everything by {a.name}"
							>{a.name}</a
						>
						{#if a.orcid}<a class="ext orcid" href="https://orcid.org/{a.orcid}" target="_blank" rel="noreferrer" title="ORCID {a.orcid}">iD</a>{/if}
						{#if a.openalexId}<a class="ext" href="https://openalex.org/authors/{a.openalexId}" target="_blank" rel="noreferrer" title="OpenAlex {a.openalexId}">OA</a>{/if}
					</li>
				{/each}
			</ul>
		{/if}

		<div class="meta">
			{#if w.year}<span>{w.year}</span>{/if}
			{#if w.venue}<span class="venue">{w.venue}</span>{/if}
			{#if w.pageCount}<span>{w.pageCount} pages</span>{/if}
			{#if w.citations}<span>{w.citations.toLocaleString()} citations</span>{/if}
		</div>

		{#if w.ids.length}
			<div class="ids">
				{#each w.ids as id (id.scheme + id.value)}<IdChip scheme={id.scheme} value={id.value} />{/each}
			</div>
		{/if}

		<div class="actions">
			<a class="btn" href={href(`/?like=${w.id}`)}>≈ Similar in library</a>
			{#if currentFile?.available}
				<button class="btn" type="button" onclick={() => reveal(currentFile.id)}>{revealing ? 'Revealed' : 'Reveal in Finder'}</button>
			{/if}
			<button class="btn ghost" type="button" onclick={() => (wide = !wide)} title="Give the PDF more room">{wide ? '⇥ Details' : '⇤ Wider PDF'}</button>
		</div>

		{#if w.abstract}
			<section>
				<h2>Abstract</h2>
				<p class="abstract">{w.abstract}</p>
			</section>
		{/if}
		{#if w.summary}
			<section>
				<h2>Summary <span class="ai">local model</span></h2>
				<p class="summary">{w.summary}</p>
			</section>
		{/if}

		{#if w.topics.length}
			<section>
				<h2>Topics</h2>
				{#if topicGroups.arxiv.length}
					<div class="tgroup">
						<span class="tlabel">arXiv</span>
						{#each topicGroups.arxiv as t (t.id)}<a class="tag cat" href={href(`/?arxiv=${encodeURIComponent(t.name)}`)}>{t.name}</a>{/each}
					</div>
				{/if}
				{#if topicGroups.openalex.length}
					<div class="tgroup">
						<span class="tlabel">OpenAlex</span>
						{#each topicGroups.openalex as t (t.id)}
							<span class="tag" title="{t.parent ?? ''} · score {t.score?.toFixed(2) ?? '?'}">{t.name}</span>
						{/each}
					</div>
				{/if}
				{#each topicGroups.llm as [field, names] (field)}
					<div class="tgroup">
						<span class="tlabel">{field}</span>
						{#each names as n (n)}<span class="tag">{n}</span>{/each}
					</div>
				{/each}
			</section>
		{/if}

		<section>
			<h2>Where it was found</h2>
			<ul class="contexts">
				{#each data.contexts as c (c.id)}
					<li class:missing={c.missingAt}>
						<div class="path mono" title={c.locator}>{c.locator.replace(/^\/Users\/[^/]+\//, '~/')}</div>
						<div class="cmeta">
							<span title="file creation time ≈ download time">{formatDate(c.occurredAt, true)}</span>
							{#if c.downloadAgent}<span>via {c.downloadAgent.replace(/^com\.apple\.Safari.*$/, 'Safari')}</span>{/if}
							{#if c.missingAt}<span class="bad">missing since {formatDate(c.missingAt)}</span>{/if}
						</div>
						{#if c.whereFroms.length}
							<div class="froms">
								{#each c.whereFroms as u, i (i)}
									<a href={u} target="_blank" rel="noreferrer" title={u}>
										<span class="faint">{i === 0 ? 'downloaded from' : 'linked from'}</span>
										{hostOf(u)}<span class="faint">{u.replace(/^https?:\/\/[^/]+/, '').slice(0, 48)}</span>
									</a>
								{/each}
							</div>
						{/if}
					</li>
				{/each}
			</ul>
		</section>

		{#if w.files.length}
			<section>
				<h2>Files</h2>
				<ul class="files">
					{#each w.files as f (f.id)}
						{@const meta = data.fileMeta.find((m) => m.id === f.id)}
						<li class:current={f.id === currentFile?.id}>
							<button type="button" class="fbtn" onclick={() => ((viewFile = f.id), (viewPage = 1))} disabled={!f.available}>
								<span class="fthumb"><Thumb fileId={f.id} width={160} /></span>
								<span class="fbody">
									<span class="fname">{f.filename ?? `file #${f.id}`}</span>
									<span class="fmeta">
										<span class="rel">{f.relation}{f.version ? ` ${f.version}` : ''}</span>
										{f.pageCount ?? '?'} pp · {formatSize(f.size)} · text {f.textStatus}
										{#if data.pagesWithText[f.id]}({data.pagesWithText[f.id]} pp indexed){/if}
										{#if !f.available}<span class="bad">· not on disk</span>{/if}
									</span>
									{#if meta?.pdfTitle || meta?.pdfCreator}
										<span class="fmeta faint">
											{#if meta.pdfTitle}PDF title “{meta.pdfTitle}” ·{/if}
											{meta.pdfCreator ?? ''}{meta.pdfProducer ? ` / ${meta.pdfProducer}` : ''}
										</span>
									{/if}
									{#if meta}<span class="fmeta faint mono">sha256 {meta.sha256.slice(0, 16)}…</span>{/if}
								</span>
							</button>
						</li>
					{/each}
				</ul>
			</section>
		{/if}

		{#if data.similar.length}
			<section>
				<h2>Similar in your library</h2>
				<ul class="similar">
					{#each data.similar as s (s.work.id)}
						<li>
							<a href={href(`/work/${s.work.id}`)} class="stitle">{s.work.title ?? 'Untitled'}</a>
							<span class="score">{s.score.toFixed(2)}</span>
							<div class="smeta">
								{authorLine(s.work.authors, 3)}{s.work.year ? ` · ${s.work.year}` : ''}
								{#if s.terms.length}<span class="faint"> · shared: {s.terms.join(', ')}</span>{/if}
							</div>
						</li>
					{/each}
				</ul>
				<a class="more" href={href(`/?like=${w.id}`)}>Rank whole library by similarity →</a>
			</section>
		{/if}

		{#if data.terms.length}
			<section>
				<h2>Distinctive terms</h2>
				<div class="terms">
					{#each data.terms as t (t.term)}
						<a
							href={href(`/?q=${encodeURIComponent(t.term.includes(' ') ? `"${t.term}"` : t.term)}`)}
							style:opacity={0.45 + 0.55 * (t.weight / maxTerm)}
							style:font-size="{11 + 4 * (t.weight / maxTerm)}px">{t.term}</a
						>
					{/each}
				</div>
			</section>
		{/if}

		<section>
			<details class="provd">
				<summary><h2>Provenance</h2></summary>
				<Provenance assertions={data.assertions} />
			</details>
		</section>
	</div>

	<div class="pdf">
		{#if currentFile && viewFile != null}
			<PdfViewer fileId={viewFile} pageCount={currentFile.pageCount} bind:page={viewPage} available={currentFile.available} />
		{:else}
			<div class="nofile muted">No file for this work.</div>
		{/if}
	</div>
</div>

<style>
	.work {
		display: grid;
		grid-template-columns: minmax(380px, 40%) 1fr;
		height: 100vh; /* no header above */
	}
	.work.wide {
		grid-template-columns: minmax(320px, 26%) 1fr;
	}
	.info {
		overflow-y: auto;
		padding: 16px 26px 60px;
		border-right: 1px solid var(--border);
	}
	.pdf {
		min-width: 0;
		min-height: 0;
	}
	.nav {
		display: flex;
		justify-content: space-between;
		font-size: 12.5px;
		margin-bottom: 14px;
	}
	.kind {
		display: flex;
		gap: 8px;
		align-items: center;
		font-size: 12px;
	}
	.status {
		color: var(--muted);
		font-size: 11.5px;
	}
	.status.confirmed {
		color: var(--good);
	}
	.status.rejected {
		color: var(--bad);
	}
	h1 {
		font-family: var(--serif);
		font-size: 25px;
		line-height: 1.25;
		color: var(--text-strong);
		margin: 8px 0 6px;
		font-weight: 600;
	}
	.subtitle {
		margin: 0 0 6px;
		color: var(--muted);
		font-family: var(--serif);
		font-size: 16px;
	}
	.authors {
		list-style: none;
		padding: 0;
		margin: 6px 0;
		display: flex;
		flex-wrap: wrap;
		gap: 4px 14px;
		font-size: 13.5px;
	}
	.authors li {
		display: inline-flex;
		align-items: center;
		gap: 4px;
	}
	.authors li > a:first-child {
		color: var(--text);
	}
	.authors li > a:first-child:hover {
		color: var(--accent);
	}
	.ext {
		font-size: 9px;
		font-weight: 700;
		border-radius: 3px;
		padding: 0 3px;
		line-height: 14px;
		background: var(--panel-2);
		color: var(--muted);
	}
	.ext.orcid {
		color: #a6ce39;
	}
	.ext:hover {
		text-decoration: none;
		color: var(--text);
	}
	.meta {
		display: flex;
		flex-wrap: wrap;
		gap: 4px 14px;
		color: var(--muted);
		font-size: 12.5px;
	}
	.venue {
		font-style: italic;
	}
	.ids {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
		margin-top: 12px;
	}
	.actions {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
		margin-top: 14px;
	}
	section {
		margin-top: 24px;
	}
	h2 {
		display: inline-flex;
		align-items: center;
		gap: 8px;
		font-size: 11px;
		text-transform: uppercase;
		letter-spacing: 0.08em;
		color: var(--muted);
		margin: 0 0 8px;
		font-weight: 600;
	}
	.ai {
		font-size: 9.5px;
		color: var(--accent-2);
		border: 1px solid color-mix(in srgb, var(--accent-2) 40%, transparent);
		border-radius: 3px;
		padding: 0 4px;
		letter-spacing: 0.05em;
	}
	.abstract,
	.summary {
		margin: 0;
		font-size: 13.5px;
		line-height: 1.65;
		color: #c3cad5;
	}
	.summary {
		color: #b0a8c8;
	}
	.tgroup {
		display: flex;
		flex-wrap: wrap;
		align-items: baseline;
		gap: 5px;
		margin-bottom: 6px;
	}
	.tlabel {
		font-size: 11px;
		color: var(--faint);
		min-width: 70px;
	}
	.tag {
		font-size: 11.5px;
		color: var(--text);
		background: var(--panel);
		border-radius: 4px;
		padding: 0 6px;
		line-height: 20px;
	}
	.tag.cat {
		font-family: var(--mono);
		color: var(--accent-2);
		font-size: 11px;
	}
	.contexts,
	.files,
	.similar {
		list-style: none;
		margin: 0;
		padding: 0;
	}
	.contexts li {
		padding: 8px 10px;
		background: var(--bg-raised);
		border: 1px solid var(--border);
		border-radius: 6px;
		margin-bottom: 6px;
	}
	.contexts li.missing {
		opacity: 0.6;
	}
	.path {
		color: var(--text);
		overflow-wrap: anywhere;
	}
	.cmeta {
		display: flex;
		gap: 10px;
		font-size: 11.5px;
		color: var(--muted);
		margin-top: 2px;
	}
	.froms {
		display: flex;
		flex-direction: column;
		margin-top: 4px;
		font-size: 12px;
	}
	.froms a {
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.bad {
		color: var(--bad);
	}
	.files li {
		margin-bottom: 6px;
	}
	.fbtn {
		display: flex;
		gap: 10px;
		width: 100%;
		text-align: left;
		background: var(--bg-raised);
		border: 1px solid var(--border);
		border-radius: 6px;
		padding: 8px;
	}
	.files li.current .fbtn {
		border-color: var(--accent);
	}
	.fbtn:disabled {
		cursor: default;
		opacity: 0.7;
	}
	.fthumb {
		width: 40px;
		flex: none;
	}
	.fbody {
		display: flex;
		flex-direction: column;
		min-width: 0;
		gap: 1px;
	}
	.fname {
		font-size: 12.5px;
		color: var(--text-strong);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.fmeta {
		font-size: 11.5px;
		color: var(--muted);
	}
	.rel {
		text-transform: uppercase;
		font-size: 10px;
		letter-spacing: 0.05em;
		color: var(--accent);
		margin-right: 4px;
	}
	.similar li {
		padding: 6px 0;
		border-bottom: 1px solid var(--border);
	}
	.stitle {
		font-family: var(--serif);
		font-size: 14px;
		color: var(--text-strong);
	}
	.score {
		float: right;
		font-family: var(--mono);
		font-size: 11px;
		color: var(--good);
	}
	.smeta {
		font-size: 11.5px;
		color: var(--muted);
	}
	.more {
		display: inline-block;
		margin-top: 8px;
		font-size: 12.5px;
	}
	.terms {
		display: flex;
		flex-wrap: wrap;
		gap: 4px 10px;
		align-items: baseline;
	}
	.terms a {
		color: var(--text);
	}
	.terms a:hover {
		color: var(--accent);
	}
	.provd summary {
		cursor: pointer;
		list-style: none;
	}
	.provd summary::before {
		content: '▸ ';
		color: var(--muted);
		font-size: 11px;
	}
	.provd[open] summary::before {
		content: '▾ ';
	}
	.nofile {
		padding: 40px;
	}
</style>
