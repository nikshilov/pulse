/**
 * Reactive lab state. Native EventTarget — same pattern as Hearth.
 * Components subscribe; mutations go through methods on this singleton.
 */
import type { Emotion, FixtureEvent } from './fixture.js';
import type { RetrieveResult, UserState } from './scorer.js';

export type LabEvent =
  | 'queryChanged'
  | 'stateChanged'
  | 'resultsChanged'
  | 'corpusChanged';

export class LabState extends EventTarget {
  query = '';
  userState: UserState = {
    mood_vector: {},
    hrv: 65,
    stress_proxy: 0.3,
    sleep_quality: 0.7,
  };
  results: RetrieveResult[] = [];
  corpus: FixtureEvent[] = [];
  highlightId: number | null = null;

  setQuery(q: string): void {
    this.query = q;
    this.dispatchEvent(new CustomEvent('queryChanged', { detail: q }));
  }

  setMood(emotion: Emotion, value: number): void {
    this.userState.mood_vector = { ...this.userState.mood_vector, [emotion]: value };
    this.dispatchEvent(new CustomEvent('stateChanged', { detail: this.userState }));
  }

  setBiometric(field: 'hrv' | 'stress_proxy' | 'sleep_quality', value: number): void {
    this.userState = { ...this.userState, [field]: value };
    this.dispatchEvent(new CustomEvent('stateChanged', { detail: this.userState }));
  }

  resetState(): void {
    this.userState = {
      mood_vector: {},
      hrv: 65,
      stress_proxy: 0.3,
      sleep_quality: 0.7,
    };
    this.dispatchEvent(new CustomEvent('stateChanged', { detail: this.userState }));
  }

  randomizeState(): void {
    const emotions: Emotion[] = ['joy', 'sadness', 'anger', 'fear', 'shame', 'trust'];
    const mood: Partial<Record<Emotion, number>> = {};
    // Weight 1-2 emotions strongly, others zero — like real moods
    const primary = emotions[Math.floor(Math.random() * emotions.length)];
    mood[primary] = 0.5 + Math.random() * 0.4;
    if (Math.random() > 0.5) {
      const secondary = emotions[Math.floor(Math.random() * emotions.length)];
      if (secondary !== primary) mood[secondary] = 0.3 + Math.random() * 0.3;
    }
    this.userState = {
      mood_vector: mood,
      hrv: 35 + Math.random() * 50,
      stress_proxy: Math.random(),
      sleep_quality: Math.random(),
    };
    this.dispatchEvent(new CustomEvent('stateChanged', { detail: this.userState }));
  }

  setResults(r: RetrieveResult[]): void {
    this.results = r;
    this.dispatchEvent(new CustomEvent('resultsChanged', { detail: r }));
  }

  setCorpus(c: FixtureEvent[]): void {
    this.corpus = c;
    this.dispatchEvent(new CustomEvent('corpusChanged', { detail: c }));
  }

  setHighlight(id: number | null): void {
    this.highlightId = id;
    this.dispatchEvent(new CustomEvent('resultsChanged', { detail: this.results }));
  }
}

export const labState = new LabState();
