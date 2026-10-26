<script lang="ts">
	// Renders text whose highlighted spans are delimited by \u0002 … \u0003
	// (as produced by FTS5 highlight()/snippet()), without using {@html}.
	let { text }: { text: string } = $props();

	const parts = $derived.by(() => {
		const out: { t: string; hl: boolean }[] = [];
		let hl = false;
		let buf = '';
		for (const ch of text) {
			if (ch === '\u0002' || ch === '\u0003') {
				if (buf) out.push({ t: buf, hl });
				buf = '';
				hl = ch === '\u0002';
			} else buf += ch;
		}
		if (buf) out.push({ t: buf, hl });
		return out;
	});
</script>

{#each parts as p, i (i)}{#if p.hl}<mark>{p.t}</mark>{:else}{p.t}{/if}{/each}
