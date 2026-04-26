/**
 * Orchestrator — single owner of the user-types-to-AI-replies flow.
 * Composer dispatches a `composer:send` event; orchestrator handles:
 *   1. ingest user text into Pulse (parallel)
 *   2. recall against Pulse with current user_state
 *   3. start assistant message, stream LLM with retrieved context
 *   4. attach retrieval meta to assistant message on stream complete
 *   5. log router decision
 *
 * Anti-MF0 rule: this file caps at ~150 lines. Routing logic lives here so
 * components stay pure render fns.
 */
import type { PulseClient } from './api.js';
import type { ClaudeAdapter } from './llm.js';
import type { AppState } from './state.js';
import { pulseBurst } from './components/heart-pulse.js';

interface OrchestratorDeps {
  pulse: PulseClient;
  llm: ClaudeAdapter | null;
  state: AppState;
}

let deps: OrchestratorDeps | null = null;

export function setOrchestrator(d: OrchestratorDeps): void {
  deps = d;
  // Subscribe globally — composer dispatches at document level
  document.addEventListener('composer:send', onComposerSend as EventListener);
}

async function onComposerSend(ev: Event): Promise<void> {
  if (!deps) return;
  const detail = (ev as CustomEvent<{ text: string }>).detail;
  const text = detail.text.trim();
  if (!text) return;

  const { pulse, llm, state } = deps;

  state.appendUser(text);

  // Fire ingest in parallel; don't await
  pulse
    .ingest([{ text, ts: new Date().toISOString() }])
    .catch((e) => {
      console.error('ingest failed:', e);
      state.appendSystem(`ingest error: ${e.message}`);
    });

  let retrieval;
  try {
    pulseBurst(); // heart "speeds up" while we retrieve
    retrieval = await pulse.recall({
      query: text,
      mode: 'auto',
      top_k: 5,
      user_state: state.userState,
    });
    state.appendRouterDecision(
      {
        mode: retrieval.mode_used,
        confidence: retrieval.confidence,
        classifier: retrieval.classifier,
        reasoning: retrieval.reasoning,
      },
      text,
    );
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    state.appendSystem(`recall error: ${msg}`);
    return;
  }

  if (!llm) {
    // No LLM — show retrieval result as a system message so the demo still works
    state.appendSystem(
      `[no AI key set] retrieval: mode=${retrieval.mode_used}, ` +
        `events=${retrieval.event_ids.join(',')}, ` +
        `classifier=${retrieval.classifier} (${retrieval.confidence.toFixed(2)})`,
    );
    return;
  }

  const assistantMsg = state.appendAssistantStart();

  try {
    await llm.stream({
      messages: state.messages.slice(0, -1), // history excluding the new assistant skeleton
      retrieved: retrieval,
      onChunk: (chunk) => state.appendAssistantChunk(assistantMsg.id, chunk),
      onComplete: () => {
        state.attachRetrievalMeta(assistantMsg.id, retrieval);
        state.finishAssistant(assistantMsg.id);
      },
      onError: (err) => {
        state.appendAssistantChunk(
          assistantMsg.id,
          `\n\n[LLM error: ${err.message}]`,
        );
        state.finishAssistant(assistantMsg.id);
      },
    });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    state.appendAssistantChunk(assistantMsg.id, `[stream error: ${msg}]`);
    state.finishAssistant(assistantMsg.id);
  }
}
