/**
 * Pulse HTTP client — talks to Pulse Go server (POST /retrieve, /ingest).
 * Mirrors `pulse/mcp/src/index.ts:pulseFetch` shape; keep these in sync.
 *
 * On iOS (when bundled in WKWebView), routes through the `pulse_http` Swift
 * bridge instead of direct fetch (CORS + ATS friction). Detect via
 * `window.webkit?.messageHandlers?.pulse_http`.
 */

export type QueryMode = 'auto' | 'factual' | 'empathic' | 'chain';

export interface UserState {
  mood_vector?: Record<string, number>;
  sleep_quality?: number;
  sleep_hours?: number;
  hrv?: number;
  hr_trend?: string;
  hrv_trend?: string;
  stress_proxy?: number;
  recent_life_events_7d?: string[];
  time_of_day?: string;
  snapshot_days_ago?: number;
}

export interface RetrieveRequest {
  query: string;
  mode?: QueryMode;
  top_k?: number;
  user_state?: UserState;
}

export interface RetrieveResponse {
  event_ids: number[];
  mode_used: string;
  confidence: number;
  classifier: string;
  reasoning?: string;
}

export interface RouteDecision {
  mode: string;
  confidence: number;
  classifier: string;
  reasoning?: string;
}

export interface IngestObservation {
  text: string;
  ts?: string;
  scope?: string;
}

export interface PulseConfig {
  baseUrl: string;
  apiKey: string;
}

declare global {
  interface Window {
    webkit?: {
      messageHandlers?: {
        pulse_http?: { postMessage(msg: unknown): void };
      };
    };
    __pulseHTTPResponse?: (id: string, body: unknown) => void;
  }
}

export class PulseClient {
  constructor(private cfg: PulseConfig) {}

  async recall(req: RetrieveRequest): Promise<RetrieveResponse> {
    return await this.post<RetrieveResponse>('/retrieve', req);
  }

  async ingest(observations: IngestObservation[]): Promise<{ ok: boolean }> {
    return await this.post('/ingest', { observations });
  }

  private async post<T>(path: string, body: unknown): Promise<T> {
    if (window.webkit?.messageHandlers?.pulse_http) {
      return await iosBridgePost<T>(path, body, this.cfg);
    }
    const url = `${this.cfg.baseUrl.replace(/\/$/, '')}${path}`;
    const resp = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(this.cfg.apiKey ? { 'X-Pulse-Key': this.cfg.apiKey } : {}),
      },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      const text = await resp.text().catch(() => '');
      throw new Error(`Pulse HTTP ${resp.status} on ${path}: ${text.slice(0, 500)}`);
    }
    return (await resp.json()) as T;
  }
}

const pendingBridgeCalls = new Map<string, (body: unknown) => void>();

function iosBridgePost<T>(
  path: string,
  body: unknown,
  _cfg: PulseConfig,
): Promise<T> {
  const id = Math.random().toString(36).slice(2, 12);
  return new Promise<T>((resolve, reject) => {
    pendingBridgeCalls.set(id, (resp) => {
      const r = resp as { ok: boolean; status?: number; body?: unknown; error?: string };
      if (r.ok) resolve(r.body as T);
      else reject(new Error(r.error ?? `iOS bridge failed status=${r.status}`));
    });
    window.webkit!.messageHandlers!.pulse_http!.postMessage({
      id, path, body,
    });
    setTimeout(() => {
      if (pendingBridgeCalls.has(id)) {
        pendingBridgeCalls.delete(id);
        reject(new Error(`iOS bridge timeout on ${path}`));
      }
    }, 30_000);
  });
}

// Called by Swift via evaluateJavaScript("window.__pulseHTTPResponse(...)")
window.__pulseHTTPResponse = (id, body) => {
  const cb = pendingBridgeCalls.get(id);
  if (!cb) return;
  pendingBridgeCalls.delete(id);
  cb(body);
};

/** Build a default Pulse client from env / localStorage / sane defaults. */
export function makeClient(): PulseClient {
  const stored = localStorage.getItem('pulse:config');
  const fallback: PulseConfig = {
    baseUrl: 'http://127.0.0.1:18789',
    apiKey: '',
  };
  if (!stored) return new PulseClient(fallback);
  try {
    const parsed = JSON.parse(stored) as Partial<PulseConfig>;
    return new PulseClient({ ...fallback, ...parsed });
  } catch {
    return new PulseClient(fallback);
  }
}
