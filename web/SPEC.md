# Pulse Chat — design spec

Single-page web chat that demonstrates Pulse's unique value: **state-aware memory retrieval is visible**. When the user types, you can see top-3 events, the boost reasons, the mode router decision, and the live state vector. Same query under different mood/biometric → different events retrieved.

This is a Phase I deliverable. Phase G (hybrid Pulse retrieval) and Phase H (Pulse-MCP) are upstream prerequisites and are already merged in `pulse:main` (PR #6).

---

## Constraint commitments

- **Minimum frameworks, clean code.** Vanilla TypeScript + custom Web Components + native `EventTarget` reactive state. No React / Vue / Svelte / Next.js.
- **Pasha-style aesthetic** — STEAL: minimal `index.html` boot + ESM module delegation + decoupled rendering functions + async post-turn ingestion. AVOID: 288k-line monolith, opaque imports without barrel exports, unvalidated LLM JSON.
- **Fast iOS port.** Web build must be embedabble in WKWebView with three native bridges (HealthKit, Pulse HTTP, Keychain). Phase I.3 ships as Xcode project for personal sideload.
- **Build hygiene.** Hard cap: any single TS file > 300 lines triggers split. Every component module exports through `src/components/index.ts` barrel.

---

## Screen layout

```
┌──────────────────────────────────────────────────┐
│  pulse-chat                              ⚙ 💤   │  header (status + state hint icon)
├──────────────────────┬───────────────────────────┤
│                      │  state                     │
│   [chat messages]    │  ─────                     │
│                      │  mood: shame=0.6,fear=0.3  │
│   user: ...          │  hrv: 45ms (stressed)      │
│   ai: ...            │  recent: anya-conflict     │
│   ▾ memory used      │                            │
│      • evt#142 (+0.8)│  router log                │
│      • evt#88 (+0.6) │  ────────                  │
│                      │  q1 → factual (heuristic)  │
│                      │  q2 → empathic (state)     │
│                      │  q3 → chain (LLM 0.72)     │
│   [composer]         │                            │
└──────────────────────┴───────────────────────────┘
```

- **Left pane** — chat (default ~70% width on desktop, 100% mobile, sidebar opens via drawer button)
- **Right pane** — live state + router decision log (collapsible on mobile via drawer)
- **Each AI reply** — collapsible "▾ memory used" row showing `pulse_recall` retrieval (event IDs + boosts + mode used + reasoning)

---

## Tech surface

```
pulse/web/
├── package.json         # esbuild + typescript + @anthropic-ai/sdk
├── tsconfig.json        # ES2022, DOM lib
├── index.html           # ~120 lines, semantic HTML5, single module script
├── style.css            # ~200 lines, CSS variables, no Tailwind
├── src/
│   ├── main.ts          # boot — register elements, mount root, ~50 lines
│   ├── api.ts           # Pulse HTTP client (recall/ingest), ~80 lines
│   ├── state.ts         # EventTarget reactive state, ~60 lines
│   ├── llm.ts           # Claude streaming adapter, ~120 lines
│   └── components/
│       ├── index.ts          # barrel export
│       ├── composer.ts       # input + send button
│       ├── chat-thread.ts    # message list + scroll behavior
│       ├── chat-message.ts   # individual bubble (user/assistant/system)
│       ├── memory-row.ts     # collapsible "▾ memory used"
│       ├── state-panel.ts    # right-side live state vector
│       └── router-log.ts     # append-only timeline of router decisions
└── dist/                # esbuild output, served by dev server / bundled into iOS Resources/
```

Build: `npm run build` → esbuild bundles `src/main.ts` → `dist/app.js`. Target ≤ 30 KB minified.

Dev: `npm run dev` → esbuild watch + simple HTTP server on `localhost:5173`.

---

## Components

| Element | Lines (target) | Owns |
|---|---:|---|
| `<composer>` | ~100 | input field, send-on-enter, disable while LLM streams |
| `<chat-thread>` | ~80 | message list container, scroll anchor, observes `state.messages` |
| `<chat-message>` | ~100 | one bubble; renders user/assistant/system variants; markdown for assistant |
| `<memory-row>` | ~120 | collapsible details under assistant bubble; renders retrieval JSON |
| `<state-panel>` | ~140 | mood vector chips + biometric values + edit sliders (mock for web; HealthKit on iOS) |
| `<router-log>` | ~80 | append-only list of router decisions (mode + classifier + confidence + reasoning) |

Each component is a `class extends HTMLElement` with `connectedCallback()` for setup, `render()` for declarative DOM build, and unsubscribes from `state` on `disconnectedCallback()`.

---

## Data flow

```
1. User types → <composer> → state.appendUser(text)
2. state event "userMessage" fires →
3. api.ingest(text)         (parallel, non-blocking)
4. api.recall(text, mode='auto', user_state) →
     {event_ids, mode_used, confidence, classifier, reasoning}
5. <router-log> appends decision
6. <state-panel> updates if user_state changed
7. llm.stream({system, history, retrieved_events: [...]}) →
8. state.appendAssistantChunk(chunk) on each stream event
9. <chat-message> re-renders streaming text
10. on stream complete → state.attachRetrievalMeta(messageId, retrieval) →
11. <memory-row> renders meta (collapsed by default)
```

The chat LLM (Claude Sonnet 4.6 default, configurable) gets retrieved events injected into its system prompt as `<retrieved_memory>...</retrieved_memory>` blocks — the same convention used in Pulse's existing extraction prompts.

---

## API surfaces

### Pulse HTTP client (`src/api.ts`)

```typescript
type PulseConfig = { baseUrl: string; apiKey: string };

interface RetrieveRequest {
  query: string;
  mode?: 'auto' | 'factual' | 'empathic' | 'chain';
  top_k?: number;
  user_state?: UserState;
}

interface RetrieveResponse {
  event_ids: number[];
  mode_used: string;
  confidence: number;
  classifier: string;       // "heuristic" | "llm" | "default"
  reasoning?: string;
}

interface IngestRequest {
  observations: Array<{ text: string; ts?: string; scope?: string }>;
}

class PulseClient {
  recall(req: RetrieveRequest): Promise<RetrieveResponse>;
  ingest(req: IngestRequest): Promise<{ ok: boolean }>;
}
```

Mirrors `pulse/mcp/src/index.ts` shapes verbatim. Auth via `X-Pulse-Key` header (matches Go server's `IPCSecret`).

### LLM streaming (`src/llm.ts`)

```typescript
interface ChatTurn {
  role: 'user' | 'assistant' | 'system';
  content: string;
}

interface StreamOptions {
  model?: string;
  system: string;
  history: ChatTurn[];
  signal?: AbortSignal;
  onChunk: (text: string) => void;
  onComplete: () => void;
  onError: (err: Error) => void;
}

class ClaudeAdapter {
  stream(opts: StreamOptions): Promise<void>;
}
```

Direct Anthropic SDK with `streaming: true`. System prompt template includes:

```
You are a helpful assistant with access to the user's memory via Pulse.

Retrieved memories for this turn:
<retrieved_memory>
[event_id=142] {event_text}
[event_id=88]  {event_text}
[event_id=21]  {event_text}
</retrieved_memory>

Mode used: empathic (router classified by user state — body stressed, dominant shame).
Use these memories to ground your reply if they're relevant. Don't quote event_ids
to the user; speak naturally.
```

### Reactive state (`src/state.ts`)

```typescript
type StateEvent = 'userMessage' | 'assistantMessageStart' | 'assistantChunk'
                | 'assistantMessageComplete' | 'retrievalAttached' | 'userStateChanged';

class AppState extends EventTarget {
  messages: Message[];
  userState: UserState;
  routerLog: RouteDecision[];

  appendUser(text: string): void;
  appendAssistantStart(): string;       // returns messageId
  appendAssistantChunk(messageId: string, chunk: string): void;
  attachRetrievalMeta(messageId: string, meta: RetrieveResponse): void;
  setUserState(patch: Partial<UserState>): void;
  appendRouterDecision(d: RouteDecision): void;
}
```

Components subscribe via `state.addEventListener('userStateChanged', ...)`. No external state library.

---

## Memory-viz patterns (MVP scope)

Three patterns ship in MVP, ranked by signal-to-effort ratio:

### Pattern A — collapsible memory row (under each AI reply)

Default: collapsed with summary `▾ memory used (3 events, mode: empathic)`. Click expands:

```
▾ memory used (3 events, mode: empathic)
   • evt#142  +0.84  fear-on-hike (anchor, state-stressed boost)
   • evt#88   +0.61  anya-conflict-april
   • evt#21   +0.47  motorcycle-fall-elbow

   router: state-loaded query (shame=0.6 dominant)
   classifier: heuristic, confidence 0.85
```

### Pattern B — sidebar state vector (always visible on desktop)

```
state
─────
mood: shame=0.6, fear=0.3, sadness=0.2
hrv: 45ms ⚠ stressed
sleep_quality: 0.4 (poor)
recent_life_events_7d: anya-conflict, motorcycle-fall

[edit ↗]  ← opens mood/biometric sliders for web demo
```

### Pattern C — router decision log (sliding history)

```
router log
──────────
14:32  q1 "когда я был в страхе?"      → empathic (state)
14:34  q2 "where did I go yesterday?"   → factual (wh-pattern)
14:35  q3 "почему ссора с Аней?"        → chain (lead-to)
```

Click a row → highlights the corresponding message in chat thread.

---

## Non-goals (explicit, deferred to Phase I.5+)

- Multi-conversation switching / sidebar
- Voice input / TTS
- File attachments (text-only first)
- Authentication beyond Pulse IPC secret
- Database persistence (chat history lives in browser localStorage)
- Markdown tables / advanced rendering during streaming (defer to chunk boundaries)
- 3D-force-graph memory tree visualization (Phase I.7)

---

## Pasha STEAL/AVOID enforcement

| STEAL from MF0-1984 | How |
|---|---|
| Minimal `index.html` boot + ESM module delegation | One `<script type="module" src="/src/main.ts">` close-of-body |
| Decoupled rendering functions | Each component owns one DOM target; pure render fns |
| Async post-turn keeper (memory async, doesn't block UX) | `api.ingest` fired in parallel with `api.recall`; UI doesn't wait |

| AVOID from MF0-1984 | Counter-design |
|---|---|
| 288k-line monolith `main.js` | Hard cap: any TS file > 300 lines splits to module |
| Opaque imports requiring 20+ deep grep | Barrel: `src/components/index.ts` lists every element |
| Temp=0.12 JSON extraction without schema | Pulse `/recall` already returns JSON; we validate shape at `api.ts` boundary (manual `instanceof` checks for MVP, Zod if/when ergonomics warrant) |

---

## iOS port assumptions (Phase I.3)

The web bundle is embedded into a Swift Xcode project as `Resources/web/`. WKWebView loads `index.html` from bundle. Three `WKScriptMessageHandler` channels:

- `healthkit` — `webkit.messageHandlers.healthkit.postMessage({type:'snapshot'})` → Swift HKHealthStore reads HRV / sleep → returns JSON via `evaluateJavaScript("window.__healthkitResponse(...)")`
- `pulse_http` — `webkit.messageHandlers.pulse_http.postMessage({path,body})` → Swift `URLSession` to local Pulse engine (loopback or Tailscale `100.x.x.x`)
- `keychain` — secret storage for Pulse IPC token + Anthropic key

The web layer detects iOS via `window.webkit?.messageHandlers?.pulse_http` and routes HTTP through the bridge. On non-iOS (regular browser), uses standard `fetch` to Pulse HTTP endpoint configured in env.

This means `api.ts` has two HTTP backends:
```typescript
async function pulseFetch(path: string, body: unknown) {
  if (window.webkit?.messageHandlers?.pulse_http) {
    return await iosBridgeRequest('pulse_http', { path, body });
  }
  return await fetch(`${baseUrl}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Pulse-Key': apiKey },
    body: JSON.stringify(body),
  }).then(r => r.json());
}
```

---

## Verification (per phase)

- **I.2.1 scaffold** — `npm run build` succeeds; bundle ≤ 30 KB; `npm run dev` serves at `localhost:5173`
- **I.2.2 components** — manual smoke: send "hello" → assistant streams; click memory row → expands; state panel updates; router log appends
- **I.2.5 real-data** — Pulse Go server seeded with corpus events. Same query under different mood-vector slider settings → different top-3 event IDs in memory row. Demo-quality.
- **I.3.4 iOS sideload** — Xcode build → device sideload via free Apple ID. HealthKit permission prompt appears. State panel shows real HRV from Apple Health within 5s of granting permission.
- **I.4 demo recording** — 30-60s screen capture: same query "когда я был в страхе?" — first with neutral state → factual mode, retrieves random event; then mood slider → shame=0.6, HRV=45 → empathic mode, retrieves the actual fear-on-hike event.

---

## Risks + mitigations

1. **Vanilla custom elements get clunky on rapid state updates** — route ALL renders through `state.addEventListener` subscriptions; never mutate DOM outside element's `render()`. Escape hatch: SvelteKit rewrite is 1-2 days at this size if we hit a real wall.
2. **Streaming markdown parser breaks tables/code blocks mid-chunk** — defer markdown→HTML to chunk boundaries (newlines), or use `marked` with `gfm:true, async:true`.
3. **HealthKit first-launch permission UX scary** — pre-prompt explainer screen ("Pulse reads HRV + sleep to know what's weighing on you when").
4. **WKWebView CORS on direct fetch to localhost:18789** — route ALL HTTP through `pulse_http` Swift bridge in iOS builds; never raw fetch from web layer.
5. **Bundle size creep with Anthropic SDK** — SDK is ~20 KB minified (acceptable). If grows: switch to direct `fetch` calls to `api.anthropic.com/v1/messages`.

---

## Phase J (Twitter launch) prep

This spec finishing means Phase J is unblocked. Demo recording from I.4 is the centerpiece. One-liner install paths:
- Text users: `npx @nikshilov/pulse-mcp` (Pulse-MCP in Claude Desktop)
- Visual users: clone repo + Xcode sideload (Pulse iOS chat)
- Researchers: bench leaderboard at `https://github.com/nikshilov/bench/blob/main/paper/leaderboard.md`
