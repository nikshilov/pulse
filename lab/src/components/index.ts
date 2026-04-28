/**
 * Barrel exports + register. Same pattern as Hearth chat — one place to add.
 */
import { CorpusInspector } from './corpus-inspector.js';
import { RetrievePanel } from './retrieve-panel.js';
import { StateControls } from './state-controls.js';

export function registerComponents(): void {
  customElements.define('corpus-inspector', CorpusInspector);
  customElements.define('retrieve-panel', RetrievePanel);
  customElements.define('state-controls', StateControls);
}

export { CorpusInspector, RetrievePanel, StateControls };
