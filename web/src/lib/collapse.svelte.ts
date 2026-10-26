// Which sidebar panels are collapsed, remembered in localStorage.
// Starts empty (matching the server render) and is loaded on mount.

const KEY = 'papyrus.collapsed';

class Collapsed {
	state = $state<Record<string, boolean>>({});

	load() {
		try {
			this.state = JSON.parse(localStorage.getItem(KEY) ?? '{}');
		} catch {
			this.state = {};
		}
	}

	is(key: string): boolean {
		return !!this.state[key];
	}

	toggle(key: string) {
		this.state = { ...this.state, [key]: !this.state[key] };
		localStorage.setItem(KEY, JSON.stringify(this.state));
	}
}

export const collapsed = new Collapsed();
