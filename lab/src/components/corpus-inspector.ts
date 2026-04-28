/**
 * <corpus-inspector> — left pane. Lists fixture events with key metadata.
 * Highlights events that appeared in the last retrieval. Hovering shows
 * full text. Click → makes that event the query (poke around the corpus).
 */
import { labState } from '../state.js';
import type { FixtureEvent, Emotion } from '../fixture.js';

const EMOTION_HUE: Record<Emotion, number> = {
  joy: 50, sadness: 220, anger: 0, fear: 280, trust: 140,
  disgust: 90, anticipation: 30, surprise: 170, shame: 320, guilt: 350,
};

export class CorpusInspector extends HTMLElement {
  connectedCallback(): void {
    this.render();
    labState.addEventListener('corpusChanged', () => this.render());
    labState.addEventListener('resultsChanged', () => this.render());
  }

  private render(): void {
    const { corpus, results } = labState;
    const inResults = new Set(results.map((r) => r.event.id));
    this.innerHTML = `
      <div class="ci-head">
        <h2>corpus</h2>
        <span class="ci-count">${corpus.length} events</span>
      </div>
      <ul class="ci-list">
        ${corpus.map((e) => row(e, inResults.has(e.id))).join('')}
      </ul>
    `;
    this.querySelectorAll<HTMLElement>('li[data-id]').forEach((li) => {
      li.addEventListener('click', () => {
        const id = Number(li.dataset.id);
        const e = corpus.find((x) => x.id === id);
        if (e) labState.setQuery(e.text);
      });
    });
  }
}

function row(e: FixtureEvent, inResults: boolean): string {
  const dom = dominantEmotion(e);
  const hue = dom ? EMOTION_HUE[dom] : 220;
  const sat = dom ? 60 : 5;
  const tagsLine = e.tags.slice(0, 3).join(' · ');
  return `
    <li data-id="${e.id}" class="${inResults ? 'in-results' : ''}${e.anchor ? ' anchor' : ''}">
      <div class="ci-bar" style="background: hsl(${hue} ${sat}% 50%)"></div>
      <div class="ci-body">
        <div class="ci-text">${escape(e.text)}</div>
        <div class="ci-meta">
          <span class="ci-id">#${e.id}</span>
          <span class="ci-days">${formatDays(e.ts_days_ago)}</span>
          ${e.anchor ? '<span class="ci-anchor">★ anchor</span>' : ''}
          <span class="ci-tags">${escape(tagsLine)}</span>
        </div>
      </div>
    </li>
  `;
}

function dominantEmotion(e: FixtureEvent): Emotion | null {
  let best: Emotion | null = null;
  let bestVal = 0;
  for (const [k, v] of Object.entries(e.emotions) as [Emotion, number][]) {
    if (v > bestVal) {
      bestVal = v;
      best = k;
    }
  }
  return bestVal > 0.4 ? best : null;
}

function formatDays(d: number): string {
  if (d === 0) return 'today';
  if (d === 1) return '1d ago';
  if (d < 30) return `${d}d ago`;
  if (d < 365) return `${Math.round(d / 30)}mo ago`;
  return `${(d / 365).toFixed(1)}y ago`;
}

function escape(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
