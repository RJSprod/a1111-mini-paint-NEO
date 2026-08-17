/**
 * On-screen view of the transfer log.
 *
 * The console is not reachable on a phone or tablet, and the log file needs a
 * route the WebUI may not be serving, so the editor has to be able to show the
 * whole report itself - selectable, copyable, without developer tools.
 */

const PANEL_ID = 'minipaint_send_log_panel';

function style(element, rules) {
	for (const property of Object.keys(rules)) {
		element.style[property] = rules[property];
	}
}

function build_panel() {
	const panel = document.createElement('div');
	panel.id = PANEL_ID;
	style(panel, {
		position: 'fixed',
		inset: '0',
		zIndex: '10000',
		display: 'flex',
		flexDirection: 'column',
		background: 'rgba(0, 0, 0, 0.75)',
		padding: '12px',
		boxSizing: 'border-box',
	});

	const frame = document.createElement('div');
	style(frame, {
		display: 'flex',
		flexDirection: 'column',
		flex: '1 1 auto',
		minHeight: '0',
		background: '#1e1e1e',
		color: '#eee',
		border: '1px solid #555',
		borderRadius: '6px',
		overflow: 'hidden',
	});

	const header = document.createElement('div');
	style(header, {
		display: 'flex',
		alignItems: 'center',
		gap: '8px',
		padding: '10px 12px',
		borderBottom: '1px solid #444',
		font: '600 15px sans-serif',
	});

	const title = document.createElement('span');
	title.textContent = 'Send log';
	style(title, { flex: '1 1 auto' });

	const copy_button = document.createElement('button');
	copy_button.textContent = 'Copy all';
	const close_button = document.createElement('button');
	close_button.textContent = 'Close';

	for (const button of [copy_button, close_button]) {
		style(button, {
			font: '600 15px sans-serif',
			padding: '10px 16px',
			minHeight: '40px',
			borderRadius: '4px',
			border: '1px solid #666',
			background: '#333',
			color: '#eee',
			cursor: 'pointer',
		});
	}
	style(copy_button, { background: '#2d6cdf', borderColor: '#2d6cdf' });

	const text = document.createElement('textarea');
	text.readOnly = true;
	text.spellcheck = false;
	style(text, {
		flex: '1 1 auto',
		minHeight: '0',
		width: '100%',
		resize: 'none',
		border: '0',
		padding: '10px 12px',
		boxSizing: 'border-box',
		background: '#1e1e1e',
		color: '#ddd',
		font: '12px/1.45 monospace',
		// Wrapped rather than scrolled sideways: this is read on a phone, and
		// a line that runs off the edge is a line nobody reads.
		whiteSpace: 'pre-wrap',
		overflowWrap: 'anywhere',
		overflowY: 'auto',
	});

	const status = document.createElement('div');
	style(status, {
		padding: '8px 12px',
		borderTop: '1px solid #444',
		font: '13px sans-serif',
		color: '#aaa',
		minHeight: '18px',
	});
	status.textContent = 'Copy this and attach it to the bug report.';

	header.appendChild(title);
	header.appendChild(copy_button);
	header.appendChild(close_button);
	frame.appendChild(header);
	frame.appendChild(text);
	frame.appendChild(status);
	panel.appendChild(frame);

	close_button.onclick = () => panel.remove();
	copy_button.onclick = async () => {
		status.textContent = (await copy(text)) || 'Copied.';
	};

	document.body.appendChild(panel);
	return { panel, text, status };
}

/**
 * Copy the report.
 *
 * A WebUI reached over plain http on a LAN is not a secure context, where the
 * clipboard API does not exist, so the old selection-based copy has to stay as
 * the fallback - that is the case on the devices that need this most.
 */
async function copy(text) {
	try {
		if (navigator.clipboard && window.isSecureContext) {
			await navigator.clipboard.writeText(text.value);
			return null;
		}
	} catch (e) {
		/* fall through to the selection */
	}

	try {
		text.readOnly = false;
		text.focus();
		text.setSelectionRange(0, text.value.length);
		const copied = document.execCommand('copy');
		text.readOnly = true;
		if (copied) {
			return null;
		}
	} catch (e) {
		/* fall through to the instruction */
	}

	return 'Could not copy automatically - the text is selected, use your keyboard or long press to copy.';
}

/** Show the report, replacing whatever the panel was showing. */
export function show_send_log(report) {
	const existing = document.getElementById(PANEL_ID);
	const parts = existing
		? { panel: existing, text: existing.querySelector('textarea'), status: existing.lastElementChild.lastElementChild }
		: build_panel();

	parts.text.value = report;
	parts.text.scrollTop = 0;
	return parts.panel;
}

export function hide_send_log() {
	const existing = document.getElementById(PANEL_ID);
	if (existing) {
		existing.remove();
	}
}
