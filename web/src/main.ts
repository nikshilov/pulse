/**
 * pulse-chat boot. Registers custom elements, then they self-mount via
 * <chat-thread>, <composer-bar>, <state-panel>, <router-log> already in
 * index.html.
 *
 * Anti-MF0 rule: this file caps at ~80 lines. Anything else lives in
 * a sibling module.
 */
import { state } from './state.js';
import { makeClient } from './api.js';
import { makeAdapter } from './llm.js';
import { registerComponents } from './components/index.js';
import { setOrchestrator } from './orchestrator.js';

registerComponents();

const pulse = makeClient();
const llm = makeAdapter();

setOrchestrator({ pulse, llm, state });

if (!llm) {
  state.appendSystem(
    'no anthropic key in localStorage["anthropic:key"]. set one in devtools and reload — until then, retrieval still works but no AI replies.',
  );
}

// Re-export for devtools poking
(window as unknown as { __pulse: unknown }).__pulse = { pulse, llm, state };
