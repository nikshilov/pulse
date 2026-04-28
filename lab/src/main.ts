/**
 * Pulse Lab boot. Loads fixture, registers components, wires header buttons.
 *
 * Anti-MF0 rule: this file caps at ~60 lines.
 */
import { labState } from './state.js';
import { FIXTURE } from './fixture.js';
import { registerComponents } from './components/index.js';

registerComponents();
labState.setCorpus(FIXTURE);

// Pre-fill query so the demo shows results on first paint.
labState.setQuery('когда я чувствовал что меня видят целиком');

const randomBtn = document.querySelector<HTMLButtonElement>('button[data-act="randomize"]');
const resetBtn = document.querySelector<HTMLButtonElement>('button[data-act="reset"]');
randomBtn?.addEventListener('click', () => labState.randomizeState());
resetBtn?.addEventListener('click', () => labState.resetState());

(window as unknown as { __lab: unknown }).__lab = { labState, FIXTURE };
