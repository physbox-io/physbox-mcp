/**
 * Vite plugin: MCP WebSocket bridge.
 *
 * Attaches a WebSocket relay to the Vite HTTP dev server at path /mcp.
 * Two client roles connect to this same endpoint:
 *   browser     – the React app running in the browser
 *   controller  – the MCP server (node server.mjs)
 *
 * Message flow:
 *   controller → relay → browser   (commands, e.g. { cmd: 'RUN_HEADLESS', ticks: 300 })
 *   browser    → relay → controller (results,  e.g. { event: 'RESULT', data: [...] })
 *
 * The relay is necessary because browsers can only be WebSocket clients, never
 * servers. The Vite dev server is the natural broker that both sides can reach.
 *
 * Usage in vite.config.ts:
 *   import { mcpBridgePlugin } from '../expt_mcp/vite-plugin-mcp-bridge'
 *   export default defineConfig({ plugins: [react(), mcpBridgePlugin()] })
 */

import type { Plugin, ViteDevServer } from 'vite';
import { WebSocketServer, WebSocket } from 'ws';
import type { IncomingMessage } from 'http';

export function mcpBridgePlugin(): Plugin {
  return {
    name: 'vite-plugin-mcp-bridge',

    configureServer(server: ViteDevServer) {
      const wss = new WebSocketServer({ noServer: true });

      const controllers = new Set<WebSocket>();
      const browsers    = new Set<WebSocket>();

      function broadcast(targets: Set<WebSocket>, data: string) {
        for (const ws of targets) {
          if (ws.readyState === WebSocket.OPEN) ws.send(data);
        }
      }

      wss.on('connection', (ws: WebSocket, req: IncomingMessage) => {
        const url  = new URL(req.url!, 'http://localhost');
        const role = url.searchParams.get('role') ?? 'controller';
        const set  = role === 'browser' ? browsers : controllers;
        set.add(ws);

        ws.send(JSON.stringify({ event: 'CONNECTED', role }));

        ws.on('message', (raw: Buffer | string) => {
          const data = raw.toString();
          // Each side's messages go to the other side
          if (role === 'browser') broadcast(controllers, data);
          else                    broadcast(browsers,    data);
        });

        ws.on('close', () => set.delete(ws));
        ws.on('error', () => set.delete(ws));
      });

      // Intercept HTTP upgrade requests for /mcp only; leave Vite HMR alone
      server.httpServer?.on('upgrade', (req, socket, head) => {
        if (req.url?.split('?')[0] !== '/mcp') return;
        wss.handleUpgrade(req, socket as import('stream').Duplex, head, (ws) => {
          wss.emit('connection', ws, req);
        });
      });
    },
  };
}
