/**
 * <retrieve-panel> — center pane. Query input + retrieved events with
 * full score breakdown. This is the centerpiece — what makes Pulse
 * Pulse: state-aware retrieval visible, inspectable.
 */
import { labState } from '../state.js';
import { retrieve, whyExplanation, type RetrieveResult } from '../scorer.js';
import type { FixtureEvent } from '../fixture.js';

const EXAMPLES = [
  'когда я чувствовал что меня видят целиком',
  'мне страшно и не сплю',
  'когда я последний раз радовался работе',
  'когда было тепло и тихо',
  'почему я опять молчу когда надо говорить',
];

export class RetrievePanel extends HTMLElement {
  connectedCallback(): void {
    this.render();
    labState.addEventListener('queryChanged', () => {
      this.runRetrieve();
      this.syncQueryInput();
    });
    labState.addEventListener('stateChanged', () => this.runRetrieve());
    labState.addEventListener('resultsChanged', () => this.renderResults());
    this.runRetrieve();
  }

  private render(): void {
    this.innerHTML = `
      <div class="rp-head">
        <h2>retrieve</h2>
        <span class="rp-engine">pulse-lab simulator · v3-style scoring</span>
      </div>
      <div class="rp-query">
        <textarea
          class="rp-query-input"
          placeholder="ask pulse — &quot;когда я чувствовал что меня видят&quot;"
          rows="2"
        ></textarea>
        <div class="rp-examples">
          <span class="rp-ex-label">try:</span>
          ${EXAMPLES.map((q) => `<button class="rp-ex" data-q="${escape(q)}">${escape(q)}</button>`).join('')}
        </div>
      </div>
      <div class="rp-results"></div>
    `;
    const ta = this.querySelector<HTMLTextAreaElement>('.rp-query-input')!;
    ta.value = labState.query;
    ta.addEventListener('input', () => labState.setQuery(ta.value));

    this.querySelectorAll<HTMLButtonElement>('.rp-ex').forEach((b) => {
      b.addEventListener('click', () => {
        const q = b.dataset.q ?? '';
        ta.value = q;
        labState.setQuery(q);
      });
    });
  }

  private syncQueryInput(): void {
    const ta = this.querySelector<HTMLTextAreaElement>('.rp-query-input');
    if (ta && ta.value !== labState.query) ta.value = labState.query;
  }

  private runRetrieve(): void {
    if (!labState.query.trim()) {
      labState.setResults([]);
      return;
    }
    const results = retrieve(labState.query, labState.corpus, labState.userState, 5);
    labState.setResults(results);
  }

  private renderResults(): void {
    const root = this.querySelector('.rp-results');
    if (!root) return;
    const { results, query } = labState;
    if (!query.trim()) {
      root.innerHTML = `<div class="rp-empty">type a query above (or click a corpus event on the left)</div>`;
      return;
    }
    if (results.length === 0) {
      root.innerHTML = `<div class="rp-empty">no signal — try a different query</div>`;
      return;
    }
    root.innerHTML = `
      <div class="rp-stamp">top ${results.length} · ranked by state-aware score</div>
      <ol class="rp-list">${results.map(card).join('')}</ol>
    `;
  }
}

function card(r: RetrieveResult): string {
  const e: FixtureEvent = r.event;
  const why = whyExplanation(r);
  const b = r.breakdown;
  return `
    <li class="rp-card${e.anchor ? ' anchor' : ''}">
      <div class="rp-card-head">
        <span class="rp-score">${r.score.toFixed(3)}</span>
        <span class="rp-id">#${e.id}</span>
        ${e.anchor ? '<span class="rp-anchor">★</span>' : ''}
        <span class="rp-why">${escape(why)}</span>
      </div>
      <div class="rp-text">${escape(e.text)}</div>
      <div class="rp-bars">
        ${bar('topic', b.keyword)}
        ${bar('emotion', b.emotion)}
        ${bar('state', b.state)}
        ${bar('recency', b.recency)}
        ${bar('anchor', b.anchor)}
      </div>
    </li>
  `;
}

function bar(label: string, value: number): string {
  const pct = Math.round(value * 100);
  return `
    <div class="rp-bar">
      <span class="rp-bar-label">${label}</span>
      <span class="rp-bar-track"><span class="rp-bar-fill" style="width:${pct}%"></span></span>
      <span class="rp-bar-val">${pct}</span>
    </div>
  `;
}

function escape(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
