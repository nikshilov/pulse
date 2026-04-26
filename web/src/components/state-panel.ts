/**
 * <state-panel> — right-side live state vector + biometric chips + edit
 * sliders (web-side mock; iOS bridge replaces these with real HealthKit).
 */
import { state } from '../state.js';
import type { UserState } from '../api.js';

const PLUTCHIK = [
  'joy', 'sadness', 'anger', 'fear', 'trust',
  'disgust', 'anticipation', 'surprise', 'shame', 'guilt',
] as const;

export class StatePanel extends HTMLElement {
  connectedCallback(): void {
    this.render();
    state.addEventListener(
      'userStateChanged',
      this.onChange as EventListener,
    );
  }

  disconnectedCallback(): void {
    state.removeEventListener(
      'userStateChanged',
      this.onChange as EventListener,
    );
  }

  private onChange = (): void => this.render();

  private render(): void {
    const us = state.userState;
    const moods = formatMoods(us.mood_vector ?? {});
    const stressed = isStressed(us);
    const restored = isRestored(us);
    const biometricLines = [
      us.hrv != null
        ? `<span class="${stressed ? 'stressed' : restored ? 'restored' : ''}">hrv: ${us.hrv.toFixed(0)}ms${stressed ? ' ⚠ stressed' : restored ? ' ✓ restored' : ''}</span>`
        : '',
      us.sleep_quality != null
        ? `sleep: ${(us.sleep_quality * 100).toFixed(0)}%${us.sleep_quality <= 0.4 ? ' ⚠ poor' : ''}`
        : '',
      us.stress_proxy != null
        ? `stress: ${(us.stress_proxy * 100).toFixed(0)}%`
        : '',
    ]
      .filter(Boolean)
      .join('<br/>');

    this.innerHTML = `
      <h2>state</h2>

      <div class="section">
        <div class="section-label">mood</div>
        <div>${moods || '<span class="chip">neutral</span>'}</div>
      </div>

      <div class="section">
        <div class="section-label">biometric</div>
        <div class="biometric">${biometricLines || '<span style="color: var(--fg-dim)">no biometric data</span>'}</div>
      </div>

      <div class="controls">
        <div class="section-label">demo controls</div>
        ${PLUTCHIK.map((k) => sliderRow(k, us.mood_vector?.[k] ?? 0)).join('')}
        ${biometricSlider('hrv', us.hrv ?? 65, 30, 100)}
        ${biometricSlider('stress_proxy', us.stress_proxy ?? 0.3, 0, 1, 0.05)}
        ${biometricSlider('sleep_quality', us.sleep_quality ?? 0.7, 0, 1, 0.05)}
      </div>
    `;

    this.bindControls();
  }

  private bindControls(): void {
    this.querySelectorAll<HTMLInputElement>('input[type="range"]').forEach(
      (input) => {
        input.addEventListener('input', () => {
          const key = input.dataset.key!;
          const kind = input.dataset.kind!;
          const value = parseFloat(input.value);
          const out = this.parentElement?.querySelector(
            `[data-out="${key}"]`,
          ) as HTMLElement | null;
          if (out) out.textContent = formatValue(kind, value);
          if (kind === 'mood') {
            state.setUserState({
              mood_vector: { ...state.userState.mood_vector, [key]: value },
            });
          } else {
            state.setUserState({ [key]: value } as Partial<UserState>);
          }
        });
      },
    );
  }
}

function sliderRow(key: string, value: number): string {
  return `
    <label>
      <span>${key}</span>
      <input type="range" min="0" max="1" step="0.05" value="${value}"
             data-key="${key}" data-kind="mood" />
      <span data-out="${key}">${value.toFixed(2)}</span>
    </label>
  `;
}

function biometricSlider(
  key: string,
  value: number,
  min: number,
  max: number,
  step = 1,
): string {
  return `
    <label>
      <span>${key}</span>
      <input type="range" min="${min}" max="${max}" step="${step}" value="${value}"
             data-key="${key}" data-kind="biometric" />
      <span data-out="${key}">${formatValue('biometric', value)}</span>
    </label>
  `;
}

function formatValue(kind: string, v: number): string {
  return kind === 'mood' ? v.toFixed(2) : v < 1 ? v.toFixed(2) : v.toFixed(0);
}

function formatMoods(mv: Record<string, number>): string {
  return Object.entries(mv)
    .filter(([, v]) => v >= 0.2)
    .sort((a, b) => b[1] - a[1])
    .map(([k, v]) => {
      const strength = v >= 0.5 ? 'high' : v >= 0.3 ? 'medium' : 'low';
      return `<span class="chip" data-strength="${strength}">${k}=${v.toFixed(2)}</span>`;
    })
    .join('');
}

function isStressed(us: UserState): boolean {
  if (us.stress_proxy != null && us.stress_proxy >= 0.6) return true;
  if (us.sleep_quality != null && us.sleep_quality <= 0.4) return true;
  if (us.hrv != null && us.hrv < 55) return true;
  return false;
}

function isRestored(us: UserState): boolean {
  return (
    us.stress_proxy != null &&
    us.stress_proxy <= 0.3 &&
    (us.sleep_quality == null || us.sleep_quality >= 0.7)
  );
}
