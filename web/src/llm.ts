/**
 * Claude streaming adapter. Wraps Anthropic SDK's streaming Messages API.
 *
 * Phase I.2 MVP: uses `dangerouslyAllowBrowser: true` for direct browser
 * calls during development. Phase J / production deployment should proxy
 * through Pulse Go server `/chat/stream` (TODO endpoint, post-launch) to
 * keep API keys off the client.
 *
 * Anti-MF0 rule: this file caps at ~150 lines. If we need more chat features
 * (tool use, vision, system prompt builder), they go in sibling files.
 */
import Anthropic from '@anthropic-ai/sdk';
import type { Message } from './state.js';
import type { RetrieveResponse } from './api.js';

export interface LLMConfig {
  apiKey: string;
  model?: string;
  maxTokens?: number;
  baseSystem?: string;
}

export interface StreamArgs {
  messages: Message[];
  retrieved?: RetrieveResponse;
  retrievedTexts?: Map<number, string>;
  onChunk: (text: string) => void;
  onComplete: () => void;
  onError: (err: Error) => void;
  signal?: AbortSignal;
}

const DEFAULT_SYSTEM = `You are a helpful assistant with access to the user's memory via Pulse.

Use any retrieved memories provided in <retrieved_memory> blocks to ground your replies — but speak naturally; don't quote event_ids to the user.

If the retrieval is empty or low-confidence, respond from general knowledge and let the user know you don't have specific memory of what they're asking about.`;

export class ClaudeAdapter {
  private client: Anthropic;
  private cfg: LLMConfig;

  constructor(cfg: LLMConfig) {
    this.cfg = cfg;
    this.client = new Anthropic({
      apiKey: cfg.apiKey,
      dangerouslyAllowBrowser: true,
    });
  }

  async stream(args: StreamArgs): Promise<void> {
    try {
      const system = this.buildSystem(args.retrieved, args.retrievedTexts);
      const apiMessages = args.messages
        .filter((m) => m.role !== 'system' && m.text.trim())
        .map((m) => ({
          role: m.role as 'user' | 'assistant',
          content: m.text,
        }));

      const stream = await this.client.messages.stream({
        model: this.cfg.model ?? 'claude-sonnet-4-6',
        max_tokens: this.cfg.maxTokens ?? 2048,
        system,
        messages: apiMessages,
      });

      stream.on('text', (text) => args.onChunk(text));
      stream.on('error', (err) => args.onError(err as Error));
      await stream.finalMessage();
      args.onComplete();
    } catch (err) {
      args.onError(err instanceof Error ? err : new Error(String(err)));
    }
  }

  private buildSystem(
    retrieved: RetrieveResponse | undefined,
    texts: Map<number, string> | undefined,
  ): string {
    const parts: string[] = [this.cfg.baseSystem ?? DEFAULT_SYSTEM];

    if (retrieved && retrieved.event_ids.length > 0) {
      const lines = retrieved.event_ids.map((id) => {
        const text = texts?.get(id);
        return text
          ? `[event_id=${id}] ${text}`
          : `[event_id=${id}] (text not loaded; reference by id only)`;
      });
      parts.push(
        '',
        '<retrieved_memory>',
        ...lines,
        '</retrieved_memory>',
        '',
        `Mode used: ${retrieved.mode_used} (router: ${retrieved.classifier}, confidence ${retrieved.confidence.toFixed(2)})`,
      );
    } else if (retrieved) {
      parts.push(
        '',
        '<retrieved_memory>(no memories matched)</retrieved_memory>',
      );
    }

    return parts.join('\n');
  }
}

export function makeAdapter(): ClaudeAdapter | null {
  const apiKey =
    localStorage.getItem('anthropic:key') ??
    (window as unknown as { ANTHROPIC_API_KEY?: string }).ANTHROPIC_API_KEY;
  if (!apiKey) return null;
  return new ClaudeAdapter({ apiKey });
}
