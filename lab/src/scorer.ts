/**
 * Pulse-like retrieval simulator.
 *
 * NOT the real Pulse engine — this is a JS stand-in so the lab works
 * zero-config in the browser. The shape and intent mirror Pulse v3:
 *
 *   final_score =
 *     keyword_match × 0.35           (lexical / topical)
 *   + emotion_match(state, event) × 0.30
 *   + state_boost(state, event) × 0.15
 *   + recency_factor × 0.10
 *   + anchor_bonus × 0.10
 *
 * The point: changing user_state changes top-k. That's the demo.
 *
 * Real engine: Go binary in ../internal/, exposes HTTP /retrieve.
 * If users want the real thing — point Hearth at it.
 *
 * Anti-MF0 rule: this file caps at ~150 lines.
 */
import type { Emotion, FixtureEvent } from './fixture.js';

export interface UserState {
  mood_vector: Partial<Record<Emotion, number>>;
  hrv?: number; // ms (40 stressed → 80 calm)
  stress_proxy?: number; // 0..1
  sleep_quality?: number; // 0..1
}

export interface RetrieveResult {
  event: FixtureEvent;
  score: number;
  breakdown: {
    keyword: number;
    emotion: number;
    state: number;
    recency: number;
    anchor: number;
  };
}

const W = {
  keyword: 0.35,
  emotion: 0.30,
  state: 0.15,
  recency: 0.10,
  anchor: 0.10,
};

export function retrieve(
  query: string,
  events: FixtureEvent[],
  state: UserState,
  topK = 5,
): RetrieveResult[] {
  const tokens = tokenize(query);
  const stateMag = magnitude(Object.values(state.mood_vector));
  const stressed = (state.hrv !== undefined && state.hrv < 50) ||
    (state.stress_proxy !== undefined && state.stress_proxy > 0.6);

  const scored = events.map((event) => {
    const kw = keywordScore(tokens, event);
    const em = emotionMatch(state.mood_vector, event.emotions);
    const st = stateBoost(state, event, stressed);
    const rc = recencyFactor(event.ts_days_ago);
    const an = event.anchor ? 1.0 : 0.0;

    const breakdown = {
      keyword: kw,
      emotion: em,
      state: st,
      recency: rc,
      anchor: an,
    };

    let score =
      W.keyword * kw +
      W.emotion * em +
      W.state * st +
      W.recency * rc +
      W.anchor * an;

    // When user_state has signal, lift state-resonant events more aggressively.
    if (stateMag > 0.4) score *= 1.0 + 0.4 * stateMag;

    return { event, score, breakdown };
  });

  return scored.sort((a, b) => b.score - a.score).slice(0, topK);
}

function tokenize(s: string): string[] {
  return s
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s]/gu, ' ')
    .split(/\s+/)
    .filter((t) => t.length > 2);
}

function keywordScore(queryTokens: string[], event: FixtureEvent): number {
  if (queryTokens.length === 0) return 0;
  const eventBlob = (event.text + ' ' + event.tags.join(' ')).toLowerCase();
  let matched = 0;
  for (const tok of queryTokens) {
    if (eventBlob.includes(tok)) matched += 1;
  }
  return matched / queryTokens.length;
}

function emotionMatch(
  user: Partial<Record<Emotion, number>>,
  evt: Partial<Record<Emotion, number>>,
): number {
  const userMag = magnitude(Object.values(user));
  if (userMag < 0.05) return 0;
  // Cosine over emotion dims that exist in either side
  const keys = new Set([...Object.keys(user), ...Object.keys(evt)]) as Set<Emotion>;
  let dot = 0;
  let evtMag = 0;
  for (const k of keys) {
    const u = user[k] ?? 0;
    const e = evt[k] ?? 0;
    dot += u * e;
    evtMag += e * e;
  }
  evtMag = Math.sqrt(evtMag);
  if (evtMag === 0) return 0;
  return Math.max(0, Math.min(1, dot / (userMag * evtMag)));
}

function stateBoost(state: UserState, event: FixtureEvent, stressed: boolean): number {
  // Stressed → favor anchor + calming events
  // Calm → favor balanced corpus
  let s = 0;
  if (stressed) {
    if (event.anchor) s += 0.6;
    if (event.tags.includes('regulation') || event.tags.includes('calm') ||
        event.tags.includes('anchor') || event.tags.includes('rest')) s += 0.4;
    if (event.valence > 0.5) s += 0.2;
  }
  if (state.sleep_quality !== undefined && state.sleep_quality < 0.4) {
    if (event.tags.includes('sleep') || event.tags.includes('fatigue')) s += 0.3;
  }
  return Math.max(0, Math.min(1, s));
}

function recencyFactor(daysAgo: number): number {
  // Plain exponential decay — λ tuned so 30d → ~0.5, 365d → ~0.05
  return Math.exp(-daysAgo / 45);
}

function magnitude(values: (number | undefined)[]): number {
  let sum = 0;
  for (const v of values) {
    if (typeof v === 'number') sum += v * v;
  }
  return Math.sqrt(sum);
}

export function whyExplanation(r: RetrieveResult): string {
  const parts: string[] = [];
  const b = r.breakdown;
  if (b.keyword > 0.5) parts.push(`topic match (${(b.keyword * 100).toFixed(0)}%)`);
  if (b.emotion > 0.5) parts.push(`emotional resonance (${(b.emotion * 100).toFixed(0)}%)`);
  else if (b.emotion > 0.2) parts.push(`emotional touch (${(b.emotion * 100).toFixed(0)}%)`);
  if (b.state > 0.3) parts.push('state-aligned');
  if (b.anchor === 1) parts.push('anchor');
  if (b.recency > 0.7) parts.push('recent');
  else if (b.recency < 0.1) parts.push('archival');
  return parts.join(' · ') || 'low signal';
}
