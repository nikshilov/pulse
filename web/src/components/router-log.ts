/**
 * <router-log> — append-only timeline of router decisions. Each row shows
 * timestamp + chosen mode + classifier + truncated reasoning.
 */
import { state } from '../state.js';

interface LogEntry {
  ts: number;
  mode: string;
  classifier: string;
  confidence: number;
  reasoning?: string;
  query: string;
}

export class RouterLog extends HTMLElement {
  connectedCallback(): void {
    this.render();
    state.addEventListener(
      'routerDecisionAppended',
      this.onAppend as EventListener,
    );
  }

  disconnectedCallback(): void {
    state.removeEventListener(
      'routerDecisionAppended',
      this.onAppend as EventListener,
    );
  }

  private onAppend = (): void => {
    this.render();
  };

  private render(): void {
    const entries = state.routerLog as LogEntry[];
    if (entries.length === 0) {
      this.innerHTML = `
        <h2>router log</h2>
        <div class="empty">router decisions appear here as you chat</div>
      `;
      return;
    }
    const rows = entries
      .slice(-20)
      .reverse()
      .map((e) => {
        const t = new Date(e.ts).toTimeString().slice(0, 5);
        const why = `${e.classifier} ${e.confidence.toFixed(2)} · ${shortQuery(e.query)}`;
        return `<div class="entry"><span class="time">${t}</span><span class="mode">${e.mode}</span><span class="why" title="${escapeAttr(e.reasoning ?? '')}">${escapeAttr(why)}</span></div>`;
      })
      .join('');
    this.innerHTML = `
      <h2>router log</h2>
      ${rows}
    `;
  }
}

function shortQuery(q: string): string {
  if (q.length <= 40) return q;
  return q.slice(0, 38) + '…';
}

function escapeAttr(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}
