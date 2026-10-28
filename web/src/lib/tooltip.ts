// Tooltips for every `title` in the app, shown after 200 ms instead of the
// browser's second or so. On hover an element's `title` moves to `data-tip`
// (so the native tooltip never shows) and a positioned `.tip` box shows it.
// Once one tooltip has shown, moving to the next element shows its tooltip at
// once, as macOS does. Installed once, by the root layout.

const DELAY = 200;
/** After a tooltip hides, the next one within this window shows without a delay. */
const WARM = 400;

export function installTooltips(): () => void {
	const tip = document.createElement('div');
	tip.className = 'tip';
	tip.setAttribute('role', 'tooltip');
	document.body.append(tip);

	let current: HTMLElement | null = null;
	let timer: ReturnType<typeof setTimeout> | undefined;
	let hiddenAt = 0;

	const text = (el: HTMLElement) => {
		// Svelte may set `title` again when its value changes: the new value wins.
		const title = el.getAttribute('title');
		if (title != null) {
			el.dataset.tip = title;
			el.removeAttribute('title');
			if (!el.hasAttribute('aria-label') && !el.textContent?.trim()) el.setAttribute('aria-label', title);
		}
		return el.dataset.tip ?? '';
	};

	const show = () => {
		if (!current?.isConnected) return hide();
		const value = text(current);
		if (!value) return;
		tip.textContent = value;
		tip.classList.add('on');
		const r = current.getBoundingClientRect();
		const t = tip.getBoundingClientRect();
		let top = r.bottom + 6;
		if (top + t.height > innerHeight - 4) top = r.top - t.height - 6;
		const left = Math.max(4, Math.min(innerWidth - t.width - 4, r.left + r.width / 2 - t.width / 2));
		tip.style.transform = `translate(${Math.round(left)}px, ${Math.round(top)}px)`;
	};

	const hide = () => {
		clearTimeout(timer);
		if (tip.classList.contains('on')) hiddenAt = performance.now();
		tip.classList.remove('on');
		current = null;
	};

	const over = (e: PointerEvent) => {
		const el = (e.target as Element | null)?.closest?.<HTMLElement>('[title], [data-tip]') ?? null;
		if (el === current) return;
		hide();
		if (!el) return;
		current = el;
		text(el);
		const warm = performance.now() - hiddenAt < WARM;
		timer = setTimeout(show, warm ? 0 : DELAY);
	};

	const out = (e: PointerEvent) => {
		if (current && !current.contains(e.relatedTarget as Node | null)) hide();
	};

	document.addEventListener('pointerover', over);
	document.addEventListener('pointerout', out);
	document.addEventListener('pointerdown', hide, true);
	document.addEventListener('keydown', hide, true);
	addEventListener('scroll', hide, true);
	addEventListener('blur', hide);
	return () => {
		hide();
		document.removeEventListener('pointerover', over);
		document.removeEventListener('pointerout', out);
		document.removeEventListener('pointerdown', hide, true);
		document.removeEventListener('keydown', hide, true);
		removeEventListener('scroll', hide, true);
		removeEventListener('blur', hide);
		tip.remove();
	};
}
