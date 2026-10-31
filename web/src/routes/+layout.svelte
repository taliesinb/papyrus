<script lang="ts">
	// No header: the Library page carries its own search (over the filters) and the
	// pipeline status (in its toolbar); a work's page links back to it.
	import '../app.css';
	import { onMount } from 'svelte';
	import { asset } from '$app/paths';
	import { installTooltips } from '#lib/tooltip.ts';
	import type { LayoutProps } from './$types';

	let { children, data }: LayoutProps = $props();

	onMount(installTooltips);

	// <html class="narrow"> while the window is under two thirds of the screen's width:
	// the List view then drops its cover thumbnails (WorkCard).
	onMount(() => {
		const fit = () => document.documentElement.classList.toggle('narrow', innerWidth < (screen.width * 2) / 3);
		fit();
		addEventListener('resize', fit);
		return () => removeEventListener('resize', fit);
	});
</script>

<svelte:head>
	<title>{data.brand}</title>
	<link rel="icon" type="image/svg+xml" href={asset('favicon.svg')} />
	<link rel="apple-touch-icon" href={asset('apple-touch-icon.png')} />
</svelte:head>

<main>
	{@render children()}
</main>

<style>
	main {
		min-height: 100vh;
	}
</style>
