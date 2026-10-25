// Local development bridge. Production continues to use gunicorn in Docker.
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { resolve } from 'node:path';

const args = process.argv.slice(2);
const value = (name, fallback) => {
  const i = args.indexOf(name);
  return i >= 0 && args[i + 1] ? args[i + 1] : fallback;
};
const localPython = resolve(process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python');
const python = process.env.PYTHON || (existsSync(localPython) ? localPython : process.env.CODEX_PRIMARY_RUNTIME_PYTHON) || 'python3';
const child = spawn(python, ['-m', 'flask', '--app', 'webapp', 'run', '--host', value('--host', '127.0.0.1'), '--port', value('--port', process.env.PORT || '8501')], { stdio: 'inherit', env: process.env });
child.on('error', error => { console.error(error.message); process.exit(1); });
child.on('exit', code => process.exit(code ?? 1));
process.on('SIGINT', () => child.kill('SIGINT'));
process.on('SIGTERM', () => child.kill('SIGTERM'));
