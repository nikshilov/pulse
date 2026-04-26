/**
 * <memory-row> — collapsible "▾ memory used" under an assistant bubble.
 * Renders top-K event_ids + mode_used + classifier + reasoning.
 */
import type { RetrieveResponse } from '../api.js';

export class MemoryRow extends HTMLElement {
  private _retrieval: RetrieveResponse | null = null;
  private body: HTMLElement | null = null;
  private toggle: HTMLButtonElement | null = null;
  private expanded = false;

  set retrieval(r: RetrieveResponse) {
    this._retrieval = r;
    this.render();
  }

  connectedCallback(): void {
    if (this._retrieval) this.render();
  }

  private render(): void {
    if (!this._retrieval) return;
    const r = this._retrieval;
    const arrow = this.expanded ? '▴' : '▾';
    const count = `${r.event_ids.length} event${r.event_ids.length === 1 ? '' : 's'}`;
    const modeIcon = MODE_ICONS[r.mode_used] ?? '✨';
    const rows = r.event_ids
      .map(
        (id, i) =>
          `<div class="row"><span class="id">evt#${id}</span><span class="score">#${i + 1}</span><span></span></div>`,
      )
      .join('');
    const meta = [
      `classifier: ${r.classifier}`,
      `confidence: ${r.confidence.toFixed(2)}`,
      r.reasoning ? `why: ${escapeHTML(r.reasoning)}` : '',
    ]
      .filter(Boolean)
      .join('  •  ');

    this.innerHTML = `
      <button type="button" class="toggle" aria-expanded="${this.expanded}">
        ${arrow} memory used <span style="opacity:0.6">·</span> ${escapeHTML(count)}
        <span class="mode-badge" data-mode="${escapeHTML(r.mode_used)}">${modeIcon} ${escapeHTML(r.mode_used)}</span>
      </button>
      <div class="body" ${this.expanded ? '' : 'hidden'}>
        ${rows}
        <div class="meta">${meta}</div>
      </div>
    `;

    this.toggle = this.querySelector('.toggle');
    this.body = this.querySelector('.body');
    this.toggle?.addEventListener('click', () => this.toggleExpand());
  }

  private toggleExpand(): void {
    this.expanded = !this.expanded;
    this.render();
  }
}

const MODE_ICONS: Record<string, string> = {
  factual: '📑',
  empathic: '💗',
  chain: '⛓',
  auto: '✨',
};

function escapeHTML(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
