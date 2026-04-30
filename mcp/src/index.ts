#!/usr/bin/env node
/**
 * Pulse MCP server.
 *
 * Exposes Pulse memory engine as MCP tools so any MCP-compatible client
 * (Claude Desktop, Cursor, mcp-agent, etc.) can use state-aware empathic
 * retrieval for free.
 *
 * Tools:
 *   - pulse_recall(query, mode?, top_k?, user_state?)
 *       Retrieves event_ids ranked by hybrid retrieval. Mode auto/factual/
 *       empathic/chain. Optional user_state biases stateful queries to
 *       body-relevant memories.
 *   - pulse_ingest(text, ts?)
 *       Adds an observation to Pulse's memory store. Pulse's extraction
 *       pipeline asynchronously builds the event graph, embeddings, and
 *       (when configured) atomic facts.
 *   - pulse_state()
 *       Returns the most recent biometric / mood snapshot known to Pulse.
 *       Useful for clients that want to read state without setting it.
 *
 * Connection: this MCP server is a thin wrapper. It does NOT contain the
 * memory engine itself — it talks to a running Pulse HTTP server (default
 * http://127.0.0.1:18789) via the same /retrieve, /ingest endpoints used
 * by Garden / Elle / livegarden.app.
 *
 * Setup:
 *   1. Run Pulse engine somewhere (`pulse server` from the Go binary,
 *      or use the hosted Pulse cloud once published).
 *   2. Configure your MCP client (e.g. Claude Desktop) with:
 *      {
 *        "command": "npx",
 *        "args": ["-y", "@nikshilov/pulse-mcp"],
 *        "env": {
 *          "PULSE_BASE_URL": "http://127.0.0.1:18789",
 *          "PULSE_API_KEY": "your-ipc-secret"
 *        }
 *      }
 */
import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from '@modelcontextprotocol/sdk/types.js';

const PULSE_BASE_URL =
  process.env.PULSE_BASE_URL ?? 'http://127.0.0.1:18789';
const PULSE_API_KEY = process.env.PULSE_API_KEY ?? '';

const VERSION = '0.1.0';

/* ────────────────────────────────────────────────────────────────────────
 * HTTP client for Pulse engine
 * ──────────────────────────────────────────────────────────────────────── */

interface RetrieveBody {
  query: string;
  mode?: 'auto' | 'factual' | 'empathic' | 'chain';
  top_k?: number;
  user_state?: Record<string, unknown>;
}

interface RetrieveResponse {
  event_ids: number[];
  mode_used: string;
  confidence: number;
  classifier: string;
  reasoning?: string;
}

interface IngestBody {
  content_text: string;
  captured_at: string;
  scope: string;
  source_kind: string;
  source_id: string;
}

async function pulseFetch<T>(path: string, body: unknown): Promise<T> {
  const url = `${PULSE_BASE_URL.replace(/\/$/, '')}${path}`;
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
  };
  if (PULSE_API_KEY) {
    headers['X-Pulse-Key'] = PULSE_API_KEY;
  }
  const resp = await fetch(url, {
    method: 'POST',
    headers,
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(
      `Pulse HTTP ${resp.status} on ${path}: ${text.slice(0, 500)}`,
    );
  }
  return (await resp.json()) as T;
}

/* ────────────────────────────────────────────────────────────────────────
 * MCP server setup
 * ──────────────────────────────────────────────────────────────────────── */

const server = new Server(
  { name: 'pulse-mcp', version: VERSION },
  { capabilities: { tools: {} } },
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: 'pulse_recall',
      description:
        "Retrieve memories from Pulse. Returns event_ids ranked by hybrid retrieval (factual / empathic / chain modes auto-routed). Use mode='empathic' for emotional/state-aware retrieval (Pulse's strength); 'factual' for date/name/list lookups; 'chain' for causal trace queries.",
      inputSchema: {
        type: 'object',
        properties: {
          query: {
            type: 'string',
            description: 'The user query to retrieve memories for.',
          },
          mode: {
            type: 'string',
            enum: ['auto', 'factual', 'empathic', 'chain'],
            description:
              "'auto' (default): router classifies. 'factual': force fact lookup. 'empathic': force state-aware. 'chain': force causal-trace.",
          },
          top_k: {
            type: 'integer',
            minimum: 1,
            maximum: 50,
            description: 'How many events to return (default 5).',
          },
          user_state: {
            type: 'object',
            description:
              'Optional current user biometric/mood snapshot. Fields: mood_vector (Plutchik-10), sleep_quality, sleep_hours, hrv, hr_trend, hrv_trend, stress_proxy, recent_life_events_7d, time_of_day, snapshot_days_ago.',
            additionalProperties: true,
          },
        },
        required: ['query'],
      },
    },
    {
      name: 'pulse_ingest',
      description:
        "Add an observation to Pulse's memory. Pulse asynchronously extracts entities, emotion tags, and atomic facts from the text. Use this to log conversation turns, journal entries, or any text the user wants remembered.",
      inputSchema: {
        type: 'object',
        properties: {
          text: {
            type: 'string',
            description: 'The text content to ingest.',
          },
          ts: {
            type: 'string',
            description:
              'ISO8601 timestamp of the observation (default: now).',
          },
          scope: {
            type: 'string',
            enum: ['nik', 'elle', 'shared'],
            description: 'Which scope to ingest into (default "shared").',
          },
        },
        required: ['text'],
      },
    },
    {
      name: 'pulse_state',
      description:
        "Returns the most recent biometric / mood snapshot known to Pulse. Useful when the AI client wants to know the user's state without setting it.",
      inputSchema: {
        type: 'object',
        properties: {},
      },
    },
  ],
}));

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;

  try {
    if (name === 'pulse_recall') {
      const body: RetrieveBody = {
        query: String(args?.query ?? ''),
      };
      if (args?.mode) body.mode = args.mode as RetrieveBody['mode'];
      if (typeof args?.top_k === 'number') body.top_k = args.top_k;
      if (args?.user_state) {
        body.user_state = args.user_state as Record<string, unknown>;
      }
      const out = await pulseFetch<RetrieveResponse>('/retrieve', body);
      return {
        content: [
          {
            type: 'text',
            text: JSON.stringify(out, null, 2),
          },
        ],
      };
    }

    if (name === 'pulse_ingest') {
      // Pulse server's Observation schema (capture.Observation) requires:
      //   source_kind, source_id (UNIQUE for dedup),
      //   captured_at (ISO8601),
      //   scope (elle|nik|shared),
      //   content_text (the actual payload — note: NOT "text")
      // We tag MCP-originated observations with source_kind="mcp" and use
      // "mcp:<iso-timestamp>" as a stable per-call identifier.
      const text = String(args?.text ?? '');
      const captured_at =
        typeof args?.ts === 'string' ? args.ts : new Date().toISOString();
      const body: IngestBody = {
        content_text: text,
        captured_at,
        source_kind: 'mcp',
        source_id: `mcp:${captured_at}`,
        scope: typeof args?.scope === 'string' ? args.scope : 'shared',
      };
      const out = await pulseFetch<{ ok: boolean; observation_id?: number }>(
        '/ingest',
        { observations: [body] },
      );
      return {
        content: [
          {
            type: 'text',
            text: JSON.stringify(out, null, 2),
          },
        ],
      };
    }

    if (name === 'pulse_state') {
      // Pulse Go server doesn't yet expose a state endpoint; return a stub
      // until pulse server.go gains a /state route in Phase H follow-up.
      return {
        content: [
          {
            type: 'text',
            text: JSON.stringify({
              status: 'unimplemented',
              note:
                "pulse_state will return the latest biometric/mood snapshot once Pulse server adds GET /state. Track progress at https://github.com/nikshilov/pulse/issues",
            }),
          },
        ],
      };
    }

    throw new Error(`Unknown tool: ${name}`);
  } catch (err: unknown) {
    const message = err instanceof Error ? err.message : String(err);
    return {
      isError: true,
      content: [{ type: 'text', text: `Tool error: ${message}` }],
    };
  }
});

/* ────────────────────────────────────────────────────────────────────────
 * Entry point
 * ──────────────────────────────────────────────────────────────────────── */

async function main(): Promise<void> {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  // Server.connect blocks; output goes to stderr to avoid corrupting MCP stdio.
  // eslint-disable-next-line no-console
  console.error(
    `[pulse-mcp v${VERSION}] connected via stdio; backing Pulse: ${PULSE_BASE_URL}`,
  );
}

main().catch((err) => {
  // eslint-disable-next-line no-console
  console.error('[pulse-mcp] fatal:', err);
  process.exit(1);
});
