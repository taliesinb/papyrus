import { resolve } from '$app/paths';

/**
 * An app path such as `/work/3?q=x` under the base path the app is served
 * from (`paths.base` in vite.config.ts: `/papyrus` unless `PAPYRUS_WEB_BASE`
 * says otherwise). Every link, `goto` and `fetch` to the app goes through this.
 */
export function href(path: string): string {
	return (resolve as (pathname: string) => string)(path.replace(/^\//, ''));
}
