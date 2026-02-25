#!/usr/bin/env node
/**
 * Local test: fetch worker URL and parse response like the dashboard does.
 * Run: node test-worker.js
 * Requires: node 18+ (built-in fetch).
 *
 * To test the dashboard locally: cd docs && python3 -m http.server 8080
 * Then open http://localhost:8080 and check the console (F12) for errors.
 */
const WORKER_URL = 'https://laundry.zhanming-wang.workers.dev/machines';

function parseList(liveMachines) {
  let list = null;
  if (Array.isArray(liveMachines)) list = liveMachines;
  else if (liveMachines && typeof liveMachines === 'object') {
    if (Array.isArray(liveMachines.machines)) list = liveMachines.machines;
    else if (Array.isArray(liveMachines.data)) list = liveMachines.data;
    else if (Array.isArray(liveMachines.results)) list = liveMachines.results;
    else {
      const vals = Object.values(liveMachines);
      for (let v = 0; v < vals.length; v++) {
        if (Array.isArray(vals[v]) && vals[v].length > 0 && typeof vals[v][0] === 'object') {
          list = vals[v];
          break;
        }
      }
    }
  }
  return list;
}

async function main() {
  console.log('Fetching', WORKER_URL, '...');
  const r = await fetch(WORKER_URL);
  console.log('Status:', r.status);
  const data = await r.json();
  const list = parseList(data);
  if (!list || list.length === 0) {
    console.log('No machine array. Response type:', typeof data);
    console.log('Keys:', data && typeof data === 'object' ? Object.keys(data) : 'n/a');
    console.log('Sample:', JSON.stringify(data, null, 2).slice(0, 500));
    process.exit(1);
  }
  const washers = list.filter(m => m && String(m.type || m.Type || '').toLowerCase() === 'washer');
  const dryers = list.filter(m => m && String(m.type || m.Type || '').toLowerCase() === 'dryer');
  console.log('Machines:', list.length, '| Washers:', washers.length, '| Dryers:', dryers.length);
  console.log('First machine keys:', list[0] ? Object.keys(list[0]) : 'n/a');
  console.log('First washer:', washers[0] ? { type: washers[0].type, stickerNumber: washers[0].stickerNumber, available: washers[0].available } : 'n/a');
  process.exit(0);
}

main().catch(err => {
  console.error('Error:', err.message);
  process.exit(1);
});
