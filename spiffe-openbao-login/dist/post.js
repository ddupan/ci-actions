'use strict';
async function post() {
  const address = (process.env.STATE_bao_address || '').replace(/\/$/, '');
  const token = process.env.STATE_bao_token || '';
  if (!address || !token) return process.stdout.write('No OpenBao token state found; nothing to revoke\n');
  const response = await fetch(`${address}/v1/auth/token/revoke-self`, {
    method: 'POST', headers: { 'x-vault-token': token }, signal: AbortSignal.timeout(15000),
  });
  if (!response.ok) throw new Error(`OpenBao revoke-self failed (${response.status})`);
  process.stdout.write('OpenBao token revoked\n');
}
post().catch((error) => { process.stderr.write(`spiffe-openbao-login post: ${error.message}\n`); process.exitCode = 1; });
