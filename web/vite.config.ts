import adapter from '@sveltejs/adapter-node';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

// The app lives under /papyrus, the path `Papyrus Server.app` publishes it at
// with `tailscale serve`, so one build serves loopback and the tailnet alike.
// PAPYRUS_WEB_BASE='' serves it at the root instead (dev or build time).
const base = (process.env.PAPYRUS_WEB_BASE ?? '/papyrus').replace(/\/+$/, '') as '' | `/${string}`;

export default defineConfig({
	plugins: [
		sveltekit({
			compilerOptions: {
				// Force runes mode for the project, except for libraries. Can be removed in svelte 6.
				runes: ({ filename }) =>
					filename.split(/[/\\]/).includes('node_modules') ? undefined : true
			},
			adapter: adapter(),
			paths: { base }
		})
	]
});
