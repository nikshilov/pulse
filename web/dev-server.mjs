// Tiny dev server: esbuild watch + simple static file server on :5173.
// No framework, no extra deps — pure Node 18+.
import { createServer } from 'node:http';
import { readFile, stat } from 'node:fs/promises';
import { join, extname, resolve } from 'node:path';
import { spawn } from 'node:child_process';

const ROOT = resolve(import.meta.dirname);
const PORT = 5173;

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js':   'application/javascript; charset=utf-8',
  '.mjs':  'application/javascript; charset=utf-8',
  '.css':  'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg':  'image/svg+xml',
  '.png':  'image/png',
  '.map':  'application/json',
};

// Spawn esbuild watch
const watcher = spawn('npx', ['esbuild',
  'src/main.ts', '--bundle', '--format=esm', '--target=es2022',
  '--sourcemap', '--watch', '--outfile=dist/app.js',
], { cwd: ROOT, stdio: 'inherit' });

process.on('SIGINT', () => { watcher.kill(); process.exit(0); });
process.on('SIGTERM', () => { watcher.kill(); process.exit(0); });

createServer(async (req, res) => {
  let url = decodeURIComponent(req.url.split('?')[0]);
  if (url === '/') url = '/index.html';
  const filePath = join(ROOT, url);
  // basic path traversal guard
  if (!filePath.startsWith(ROOT)) {
    res.writeHead(403); return res.end('forbidden');
  }
  try {
    const s = await stat(filePath);
    if (s.isDirectory()) {
      res.writeHead(404); return res.end('not found');
    }
    const buf = await readFile(filePath);
    res.writeHead(200, {
      'Content-Type': MIME[extname(filePath)] ?? 'application/octet-stream',
      'Cache-Control': 'no-store',
    });
    res.end(buf);
  } catch (e) {
    if (e.code === 'ENOENT') {
      res.writeHead(404); return res.end('not found');
    }
    res.writeHead(500); return res.end(String(e));
  }
}).listen(PORT, () => {
  console.error(`[dev] http://localhost:${PORT}`);
});
