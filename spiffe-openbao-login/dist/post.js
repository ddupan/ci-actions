'use strict';
// Revoking is best effort: the token is short-lived and expires on its own, so
// a failed revoke must never fail the job. A fresh connection per attempt gets
// past the occasional TLS handshake that stalls at the end of a job.
const ATTEMPTS = 3;

async function revoke(address, token) {
  const response = await fetch(`${address}/v1/auth/token/revoke-self`, {
    method: 'POST', headers: { 'x-vault-token': token, connection: 'close' }, signal: AbortSignal.timeout(10000),
  });
  if (!response.ok) throw new Error(`OpenBao revoke-self failed (${response.status})`);
}

async function post() {
  const address = (process.env.STATE_bao_address || '').replace(/\/$/, '');
  const token = process.env.STATE_bao_token || '';
  if (!address || !token) return process.stdout.write('No OpenBao token state found; nothing to revoke\n');
  let last;
  for (let attempt = 1; attempt <= ATTEMPTS; attempt++) {
    try {
      await revoke(address, token);
      return process.stdout.write('OpenBao token revoked\n');
    } catch (error) {
      last = error;
      process.stdout.write(`OpenBao revoke attempt ${attempt}/${ATTEMPTS} failed: ${error.message}\n`);
    }
  }
  process.stdout.write(`::warning::OpenBao token not revoked (${last.message}); it expires at the end of its TTL\n`);
}

post().catch((error) => process.stdout.write(`::warning::OpenBao token not revoked (${error.message})\n`));
