'use strict';
const fs = require('node:fs');
const crypto = require('node:crypto');
const { jwtSvid } = require('../../lib/svid.js');

const input = (name, fallback = '') => process.env[`INPUT_${name.toUpperCase()}`] || fallback;
function appendCommand(file, name, value) {
  if (!file) throw new Error(`${name} command file is unavailable`);
  const delimiter = `ci_actions_${crypto.randomUUID()}`;
  fs.appendFileSync(file, `${name}<<${delimiter}\n${value}\n${delimiter}\n`, { mode: 0o600 });
}

async function main() {
  const address = input('BAO-ADDRESS', 'https://bao.ad.ddupan.top:8200').replace(/\/$/, '');
  const role = input('ROLE');
  const audience = input('AUDIENCE', 'openbao');
  const socket = input('SOCKET-PATH', '/run/spire/agent-sockets/spire-agent.sock');
  if (!role) throw new Error('input role is required');
  const jwt = await jwtSvid(audience, socket);
  const response = await fetch(`${address}/v1/auth/jwt-spire/login`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ role, jwt }), signal: AbortSignal.timeout(15000),
  });
  if (!response.ok) throw new Error(`OpenBao login failed (${response.status}): ${(await response.text()).slice(0, 1000)}`);
  const token = (await response.json())?.auth?.client_token;
  if (!token) throw new Error('OpenBao response contained no client token');
  // Mask before exporting: every later step prints its env (BAO_TOKEN included) in the job log, which is public.
  process.stdout.write(`::add-mask::${token}\n`);
  appendCommand(process.env.GITHUB_ENV, 'BAO_ADDR', address);
  appendCommand(process.env.GITHUB_ENV, 'BAO_TOKEN', token);
  appendCommand(process.env.GITHUB_STATE, 'bao_address', address);
  appendCommand(process.env.GITHUB_STATE, 'bao_token', token);
  process.stdout.write('SPIFFE JWT-SVID exchanged for a short-lived OpenBao token\n');
}
main().catch((error) => { process.stderr.write(`spiffe-openbao-login: ${error.message}\n`); process.exitCode = 1; });
