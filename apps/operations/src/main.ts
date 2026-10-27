import http from 'node:http';
import { createApp } from './app.js';
import { notConfigured, StackStormDispatcher } from './automation.js';
import { loadConfig } from './config.js';
import { createPool } from './db.js';
import { createMetricsApp } from './metrics.js';

const log = (msg: string, extra: Record<string, unknown> = {}) => console.log(JSON.stringify({ level: 'info', msg, ...extra }));

const config = loadConfig();
const pool = createPool(config.databaseUrl, config.dbPoolMax);
const dispatcher = config.stackstorm ? new StackStormDispatcher(config.stackstorm) : notConfigured;
const server = http.createServer(createApp({ config, pool, dispatcher, webDir: config.webDir }));
server.requestTimeout = 0; // SSE streams are long-lived; headersTimeout still bounds slow clients
server.headersTimeout = 15_000;
server.keepAliveTimeout = 65_000;

// Plain HTTP on the Compose network only; Caddy terminates TLS and checks the operator password.
server.listen(config.port, () => log('operations listening', { port: config.port, stackstorm: Boolean(config.stackstorm) }));
const metricsServer = http.createServer(createMetricsApp(pool)).listen(config.metricsPort, () => log('metrics listening', { port: config.metricsPort }));
if (!config.operatorPassword) log('WARNING: OPS_TRUST_PROXY_AUTH=true and no OPS_OPERATOR_PASSWORD; mutations rely on Caddy basic auth only');
if (!config.stackstorm) log('WARNING: StackStorm not configured; accepted operations stay queued');

let stopping = false;
async function shutdown(signal: string) {
  if (stopping) return;
  stopping = true;
  log('shutting down', { signal });
  setTimeout(() => process.exit(1), 10_000).unref();
  server.closeAllConnections();
  await Promise.all([new Promise((r) => server.close(r)), new Promise((r) => metricsServer.close(r))]);
  await pool.end();
  process.exit(0);
}
process.on('SIGTERM', () => void shutdown('SIGTERM'));
process.on('SIGINT', () => void shutdown('SIGINT'));
