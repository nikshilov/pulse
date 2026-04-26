/**
 * <heart-pulse> — animated SVG heart whose pulsation rate ties to user_state.
 *
 * Mapping (heart-rate proxy → animation period):
 *   neutral / no biometric   → 60 BPM (1.0s per beat) — calm baseline
 *   stressed (low hrv / high) → 100 BPM (0.6s) — quickened
 *   restored (low stress)    → 50 BPM (1.2s) — slow & deep
 *   actively retrieving      → 130 BPM (0.46s) — burst during recall
 *
 * Visual: warm coral SVG heart with subtle radial glow, keyframed scale
 * pulse + opacity flicker. Re-emits on `userStateChanged` to retune timing.
 */
import { state } from '../state.js';
import type { UserState } from '../api.js';

const SVG = `
<svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true">
  <path d="M12 21s-7.5-4.5-9.5-9.5C1 7.5 4 4 7.5 4c1.6 0 3 .8 4.5 2.3C13.5 4.8 14.9 4 16.5 4 20 4 23 7.5 21.5 11.5 19.5 16.5 12 21 12 21z"
        fill="currentColor" />
</svg>`;

export class HeartPulse extends HTMLElement {
  private periodMs = 1000;
  private burstUntil = 0;

  connectedCallback(): void {
    this.render();
    this.retune(state.userState);
    state.addEventListener('userStateChanged', this.onState as EventListener);
    document.addEventListener('pulse:burst', this.onBurst);
  }

  disconnectedCallback(): void {
    state.removeEventListener(
      'userStateChanged',
      this.onState as EventListener,
    );
    document.removeEventListener('pulse:burst', this.onBurst);
  }

  private onState = (e: CustomEvent<UserState>): void => {
    this.retune(e.detail);
  };

  private onBurst = (): void => {
    this.burstUntil = Date.now() + 1100;
    this.setPeriod(460); // ~130 BPM burst
    setTimeout(() => {
      if (Date.now() >= this.burstUntil) this.retune(state.userState);
    }, 1100);
  };

  private retune(us: UserState): void {
    if (Date.now() < this.burstUntil) return;
    if (us.hrv != null) {
      // Lower HRV → more stressed → faster heart
      // HRV 30→100bpm, HRV 50→75bpm, HRV 70→60bpm, HRV 90→55bpm
      const hrv = Math.max(20, Math.min(120, us.hrv));
      const bpm = 130 - hrv; // crude inverse mapping
      this.setPeriod(60_000 / bpm);
      return;
    }
    if (us.stress_proxy != null && us.stress_proxy >= 0.6) {
      this.setPeriod(680); // ~88 BPM
      return;
    }
    if (
      us.stress_proxy != null &&
      us.stress_proxy <= 0.3 &&
      us.sleep_quality != null &&
      us.sleep_quality >= 0.7
    ) {
      this.setPeriod(1200); // ~50 BPM restored
      return;
    }
    this.setPeriod(1000); // 60 BPM neutral
  }

  private setPeriod(ms: number): void {
    this.periodMs = ms;
    this.style.setProperty('--heart-period', `${ms}ms`);
  }

  private render(): void {
    this.innerHTML = `<span class="heart">${SVG}</span><span class="bpm" aria-live="polite"></span>`;
    this.updateBpmLabel();
  }

  private updateBpmLabel(): void {
    const label = this.querySelector('.bpm');
    if (label) {
      const bpm = Math.round(60_000 / this.periodMs);
      label.textContent = `${bpm}`;
    }
  }
}

// Public helper for orchestrator: trigger a heart "burst" on retrieval.
export function pulseBurst(): void {
  document.dispatchEvent(new CustomEvent('pulse:burst'));
}
