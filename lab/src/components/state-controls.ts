/**
 * <state-controls> — right pane. Sliders for mood vector + biometrics.
 * The whole point of Pulse Lab: flip a slider, watch retrieve-panel re-rank.
 */
import { labState } from '../state.js';
import type { Emotion } from '../fixture.js';

const EMOTIONS: Emotion[] = [
  'joy', 'sadness', 'anger', 'fear', 'shame', 'guilt',
  'trust', 'disgust', 'anticipation', 'surprise',
];

export class StateControls extends HTMLElement {
  connectedCallback(): void {
    this.render();
    labState.addEventListener('stateChanged', () => this.syncFromState());
  }

  private render(): void {
    const s = labState.userState;
    this.innerHTML = `
      <div class="sc-head">
        <h2>state</h2>
        <span class="sc-hint">retrieval re-ranks live</span>
      </div>

      <div class="sc-section">
        <div class="sc-label">mood vector — plutchik-10</div>
        ${EMOTIONS.map((e) => sliderRow(e, s.mood_vector[e] ?? 0, 0, 1, 0.01)).join('')}
      </div>

      <div class="sc-section">
        <div class="sc-label">biometric</div>
        ${sliderRow('hrv', s.hrv ?? 65, 30, 90, 1)}
        ${sliderRow('stress_proxy', s.stress_proxy ?? 0.3, 0, 1, 0.01)}
        ${sliderRow('sleep_quality', s.sleep_quality ?? 0.7, 0, 1, 0.01)}
      </div>
    `;
    this.querySelectorAll<HTMLInputElement>('input[type="range"]').forEach((input) => {
      const field = input.dataset.field!;
      input.addEventListener('input', () => {
        const v = Number(input.value);
        const out = this.querySelector<HTMLElement>(`[data-out="${field}"]`);
        if (out) out.textContent = formatVal(field, v);
        if (field === 'hrv' || field === 'stress_proxy' || field === 'sleep_quality') {
          labState.setBiometric(field, v);
        } else {
          labState.setMood(field as Emotion, v);
        }
      });
    });
  }

  private syncFromState(): void {
    const s = labState.userState;
    this.querySelectorAll<HTMLInputElement>('input[type="range"]').forEach((input) => {
      const field = input.dataset.field!;
      let v: number;
      if (field === 'hrv') v = s.hrv ?? 65;
      else if (field === 'stress_proxy') v = s.stress_proxy ?? 0.3;
      else if (field === 'sleep_quality') v = s.sleep_quality ?? 0.7;
      else v = s.mood_vector[field as Emotion] ?? 0;
      input.value = String(v);
      const out = this.querySelector<HTMLElement>(`[data-out="${field}"]`);
      if (out) out.textContent = formatVal(field, v);
    });
  }
}

function sliderRow(field: string, value: number, min: number, max: number, step: number): string {
  return `
    <label class="sc-row">
      <span class="sc-row-label">${field}</span>
      <input type="range" min="${min}" max="${max}" step="${step}" value="${value}" data-field="${field}" />
      <span class="sc-row-val" data-out="${field}">${formatVal(field, value)}</span>
    </label>
  `;
}

function formatVal(field: string, v: number): string {
  if (field === 'hrv') return String(Math.round(v));
  return v.toFixed(2);
}
