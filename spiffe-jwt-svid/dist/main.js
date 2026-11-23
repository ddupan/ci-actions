'use strict';
const fs = require('node:fs');
const crypto = require('node:crypto');
const { jwtSvid } = require('../../lib/svid.js');

const input = (name, fallback = '') => process.env[`INPUT_${name.toUpperCase()}`] || fallback;

async function main() {
  const audience = input('AUDIENCE');
  if (!audience) throw new Error('input audience is required');
  const svid = await jwtSvid(audience, input('SOCKET-PATH', '/run/spire/agent-sockets/spire-agent.sock'));
  process.stdout.write(`::add-mask::${svid}\n`);
  const delimiter = `ci_actions_${crypto.randomUUID()}`;
  fs.appendFileSync(process.env.GITHUB_OUTPUT, `svid<<${delimiter}\n${svid}\n${delimiter}\n`, { mode: 0o600 });
  process.stdout.write(`JWT-SVID issued for audience ${audience}\n`);
}
main().catch((error) => { process.stderr.write(`spiffe-jwt-svid: ${error.message}\n`); process.exitCode = 1; });
