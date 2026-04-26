/**
 * Barrel exports for all custom elements. Anti-MF0 rule: registering each
 * component happens HERE, not scattered across modules. To add a new element,
 * add a line below — that's the whole contract.
 */
import { ComposerBar } from './composer.js';
import { ChatThread } from './chat-thread.js';
import { ChatMessage } from './chat-message.js';
import { MemoryRow } from './memory-row.js';
import { StatePanel } from './state-panel.js';
import { RouterLog } from './router-log.js';
import { HeartPulse } from './heart-pulse.js';

export function registerComponents(): void {
  customElements.define('heart-pulse', HeartPulse);
  customElements.define('composer-bar', ComposerBar);
  customElements.define('chat-thread', ChatThread);
  customElements.define('chat-message', ChatMessage);
  customElements.define('memory-row', MemoryRow);
  customElements.define('state-panel', StatePanel);
  customElements.define('router-log', RouterLog);
}

export {
  HeartPulse, ComposerBar, ChatThread, ChatMessage, MemoryRow, StatePanel, RouterLog,
};
