// pm2 apps for the live system. Secrets live in .env (gitignored); `rr` loads it from cwd.
//   pm2 start ecosystem.config.cjs && pm2 save
// Logs: pm2 logs rr-collect | ~/.pm2/logs/
const rr = (name, args, extra = {}) => ({
  name, cwd: __dirname, script: '.venv/bin/rr', args, interpreter: 'none',
  time: true, max_memory_restart: '600M', ...extra,
});

module.exports = { apps: [
  // FR3 live: discovery every 15 s, snapshots, trades, candle backlog. Always on.
  rr('rr-collect', 'collect', { autorestart: true, restart_delay: 10000 }),

  // FR1 history: resumable; exits 0 when it reaches --start, then stays stopped.
  rr('rr-backfill', '-v backfill-graduations --start 2026-08-01', {
    autorestart: true, stop_exit_codes: [0], restart_delay: 60000,
  }),

  // FR5 + FR7 daily at 00:20 UTC: label newly closed windows, rewrite reports/analysis.json.
  { name: 'rr-daily', cwd: __dirname, script: 'bash', interpreter: 'none', time: true,
    args: ['-c', '.venv/bin/rr label && .venv/bin/rr analyze && .venv/bin/rr enrich-meta'],
    autorestart: false, cron_restart: '20 0 * * *' },

  // Watchdog every 10 min: restart the collector if its heartbeat is older than 10 min (hung loop).
  { name: 'rr-watchdog', cwd: __dirname, script: 'bash', interpreter: 'none', time: true,
    args: ['-c', '.venv/bin/rr health --max-age 600 || pm2 restart rr-collect'],
    autorestart: false, cron_restart: '*/10 * * * *' },

  // Runner list web page (/meme-research/): static files from web/, refreshed every 15 min.
  // Caddy proxies /meme-research/* on the public domain to this port.
  { name: 'rr-web', cwd: __dirname, script: '.venv/bin/python', interpreter: 'none', time: true,
    args: ['-m', 'http.server', '8795', '--bind', '127.0.0.1', '--directory', 'web'], autorestart: true },
  // Live test of the runner rule (strategy.LIVE_RULE): decides each graduation at T0+60m,
  // scores signals after 72h, writes web/meme-research/live.json every 30 s.
  rr('rr-live', 'live-test', { autorestart: true, restart_delay: 30000 }),
  rr('rr-trend', 'trend', { autorestart: true, restart_delay: 30000 }),
  // Runner table (live DexScreener) + insights.json, every 15 min.
  { name: 'rr-runners', cwd: __dirname, script: 'bash', interpreter: 'none', time: true,
    args: ['-c', '.venv/bin/rr runners-page && .venv/bin/rr insights'],
    autorestart: false, cron_restart: '*/15 * * * *' },
] };
