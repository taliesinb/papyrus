// "Don't move what's under the cursor" for the filter panes.
//
// Normally a pane hides values whose count is zero (they only arise when
// something else has narrowed the set). But while the user is clicking in
// a pane, every pane's set of visible rows is frozen at what it was before
// the first click, so nothing above or under the pointer moves and a click
// can be undone in place. The freeze ends when the pointer leaves that pane,
// or, for touch / keyboard, after a short period without another click, and
// on any navigation that did not come from a pane click (a search, a sort, the
// year bars, back/forward): otherwise rows frozen while a search had narrowed
// the set would stay hidden after the search is cleared.

const IDLE_MS = 2000;

class FacetFocus {
	/** Visible values per pane at the moment the interaction started. */
	frozen = $state<Record<string, string[]> | null>(null);
	/** The pane being interacted with. */
	pinned = $state<string | null>(null);
	/** Live visible values per pane, kept up to date by the panes (not reactive). */
	visible: Record<string, string[]> = {};
	private timer: ReturnType<typeof setTimeout> | undefined;
	/** A pane click has started a navigation that should keep the freeze. */
	private fromPane = false;

	pin(key: string, pointerType: string) {
		if (this.pinned !== key) this.release();
		if (!this.frozen) this.frozen = { ...this.visible };
		this.pinned = key;
		this.fromPane = true;
		clearTimeout(this.timer);
		// No hover to end the interaction for touch or keyboard: use idle time.
		if (pointerType !== 'mouse' && pointerType !== 'pen') this.timer = setTimeout(() => this.release(), IDLE_MS);
	}

	leave(key: string) {
		if (this.pinned === key) this.release();
	}

	/** After every navigation (+page.svelte): keep the freeze only for a pane's own clicks. */
	navigated() {
		if (!this.fromPane) this.release();
		this.fromPane = false;
	}

	release() {
		clearTimeout(this.timer);
		this.frozen = null;
		this.pinned = null;
	}
}

export const facetFocus = new FacetFocus();
