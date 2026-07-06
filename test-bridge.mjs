/**
 * End-to-end test of the WebSocket relay (no browser required).
 * Connects two raw WS clients to the Vite dev server:
 *   - one pretending to be the browser (handles commands, sends results)
 *   - one pretending to be the MCP controller (sends commands, awaits results)
 */

import { WebSocket } from 'ws';

const PORT = 5173;
const URL  = `ws://localhost:${PORT}/mcp`;

function connect(role) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(`${URL}?role=${role}`);
    ws.on('open',  () => resolve(ws));
    ws.on('error', reject);
    setTimeout(() => reject(new Error(`${role} connect timeout`)), 3000);
  });
}

async function run() {
  console.log(`Connecting to ws://localhost:${PORT}/mcp ...`);

  const [browser, controller] = await Promise.all([
    connect('browser'),
    connect('controller'),
  ]);
  console.log('Both clients connected.\n');

  // ── Browser side: handle commands and send back results ──
  browser.on('message', (raw) => {
    const msg = JSON.parse(raw.toString());
    if (!msg.cmd) return;
    console.log(`[browser] received cmd: ${msg.cmd}`);

    const responses = {
      GET_STATE:    { nodes: [{ id: 'n1', type: 'machine', data: { label: 'Test' } }], edges: [], isRunning: false },
      RUN_HEADLESS: [{ id: 'n1', label: 'Test', type: 'machine', state: { processed: 17, queue: [] } }],
      START_SIM:    { ok: true },
    };
    const data = responses[msg.cmd] ?? { error: `Unknown: ${msg.cmd}` };
    browser.send(JSON.stringify({ event: 'RESULT', cmd: msg.cmd, id: msg.id, data }));
  });

  // ── Controller side: send commands, collect results ──
  const results = {};
  controller.on('message', (raw) => {
    const msg = JSON.parse(raw.toString());
    if (msg.event === 'RESULT' && results[msg.id]) {
      results[msg.id](msg.data);
    }
  });

  function send(cmd, extra = {}) {
    return new Promise((resolve) => {
      const id = Math.random().toString(36).slice(2, 8);
      results[id] = resolve;
      controller.send(JSON.stringify({ cmd, id, ...extra }));
    });
  }

  // ── Run the tests ──
  console.log('--- Test 1: GET_STATE ---');
  const state = await send('GET_STATE');
  console.log('result:', JSON.stringify(state));
  console.assert(Array.isArray(state.nodes), 'nodes is array');

  console.log('\n--- Test 2: RUN_HEADLESS ---');
  const headless = await send('RUN_HEADLESS', { ticks: 300 });
  console.log('result:', JSON.stringify(headless));
  console.assert(Array.isArray(headless), 'headless result is array');
  console.assert(headless[0].state.processed === 17, 'processed count matches');

  console.log('\n--- Test 3: START_SIM ---');
  const start = await send('START_SIM');
  console.log('result:', JSON.stringify(start));
  console.assert(start.ok === true, 'start ok');

  console.log('\n✓ All tests passed — relay works, JSON in / JSON out.\n');

  browser.close();
  controller.close();
  process.exit(0);
}

run().catch(err => {
  console.error('FAIL:', err.message);
  process.exit(1);
});
