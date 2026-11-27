'use strict';
// One JWT-SVID for `audience`, wherever the job runs:
// - Gitea runners (and anything with a SPIRE Workload API socket): spire-agent, as before.
// - GitHub Actions on the homelab ARC runners: spire-identity-exchange (homelab-infra
//   platform/sandbox-identity-exchange) swaps the runner Pod's projected token plus the job's
//   GitHub OIDC token for an SVID (stack "arc").
// - GitHub-hosted runners joined to the tailnet: the same exchange through its LAN listener, with the
//   GitHub OIDC token alone (stack "hosted"; only the identities registered for that stack match).
// Both need `permissions: id-token: write`.
const fs = require('node:fs');
const https = require('node:https');
const { spawnSync } = require('node:child_process');

const EXCHANGE = {
  arcUrl: 'https://spire-identity-exchange.spire-identity-exchange.svc/api/v1/svid/arc/jwt?format=token',
  // Ayatori LB in front of the sandbox NodePort (homelab-infra clusters/ayatori-prod/sandbox-k8s.yaml).
  hostedUrl: 'https://sandbox-k8s.lab.ddupan.top:8544/api/v1/svid/hosted/jwt?format=token',
  // The exchange serves its own X509-SVID (URI SAN only): verify that identity, not a DNS name.
  id: 'spiffe://ddupan.top/sandbox/spire-identity-exchange',
  // SPIFFE bundle endpoint with a WebPKI certificate, so the trust anchor itself is authenticated.
  bundleUrl: 'https://spire-bundle.lab.ddupan.top/',
  psatPath: '/var/run/secrets/spire-identity-exchange/token',
  oidcAudience: 'spire-identity-exchange',
};

function viaAgent(audience, socket) {
  const result = spawnSync('spire-agent', ['api', 'fetch', 'jwt', '-audience', audience,
    '-socketPath', socket, '-output', 'json'], { encoding: 'utf8', maxBuffer: 16777216 });
  if (result.status !== 0) throw new Error(`spire-agent JWT fetch failed: ${result.stderr.trim()}`);
  const jwt = JSON.parse(result.stdout)?.[0]?.svids?.[0]?.svid;
  if (!jwt) throw new Error('spire-agent returned no JWT-SVID');
  return jwt;
}

async function bundlePem() {
  const response = await fetch(EXCHANGE.bundleUrl, { signal: AbortSignal.timeout(15000) });
  if (!response.ok) throw new Error(`SPIFFE bundle fetch failed (${response.status})`);
  const pems = ((await response.json()).keys || [])
    .filter((k) => k.use === 'x509-svid' && k.x5c?.length)
    .map((k) => `-----BEGIN CERTIFICATE-----\n${k.x5c[0].match(/.{1,64}/g).join('\n')}\n-----END CERTIFICATE-----\n`);
  if (!pems.length) throw new Error('SPIFFE bundle has no X.509 authorities');
  return pems;
}

async function githubOidcToken() {
  const url = process.env.ACTIONS_ID_TOKEN_REQUEST_URL;
  const token = process.env.ACTIONS_ID_TOKEN_REQUEST_TOKEN;
  if (!url || !token) throw new Error('no GitHub OIDC token available: the job needs `permissions: id-token: write`');
  const response = await fetch(`${url}&audience=${EXCHANGE.oidcAudience}`, {
    headers: { authorization: `Bearer ${token}` }, signal: AbortSignal.timeout(15000),
  });
  if (!response.ok) throw new Error(`GitHub OIDC token request failed (${response.status})`);
  const value = (await response.json())?.value;
  if (!value) throw new Error('GitHub returned no OIDC token');
  return value;
}

function post(url, headers, body, ca) {
  return new Promise((resolve, reject) => {
    const request = https.request(url, {
      method: 'POST', headers, ca, timeout: 30000,
      // Chain verified against the SPIFFE bundle (rejectUnauthorized stays on); identity by URI SAN.
      checkServerIdentity: (_host, cert) => ((cert.subjectaltname || '').split(', ').includes(`URI:${EXCHANGE.id}`)
        ? undefined : new Error(`exchange presented ${cert.subjectaltname || 'no SAN'}, expected ${EXCHANGE.id}`)),
    }, (response) => {
      let data = '';
      response.setEncoding('utf8');
      response.on('data', (chunk) => { data += chunk; });
      response.on('end', () => resolve({ status: response.statusCode, body: data }));
    });
    request.on('timeout', () => request.destroy(new Error('exchange request timed out')));
    request.on('error', reject);
    request.end(body);
  });
}

async function viaExchange(audience) {
  // The projected token exists only in ARC runner Pods; without it, the hosted stack.
  const psat = fs.existsSync(EXCHANGE.psatPath) ? fs.readFileSync(EXCHANGE.psatPath, 'utf8').trim() : null;
  const url = psat ? EXCHANGE.arcUrl : EXCHANGE.hostedUrl;
  const ca = await bundlePem();
  let last;
  // Each GitHub OIDC token is single-use at the exchange (replay cache): fetch a fresh one per try.
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    const oidc = await githubOidcToken();
    try {
      // A one-plugin stack takes the bare token; a composite one names each part.
      const auth = psat ? `k8s=${psat}:github-actions=${oidc}` : oidc;
      const { status, body } = await post(url, {
        authorization: `Bearer ${auth}`, 'content-type': 'application/json',
      }, JSON.stringify({ audiences: [audience] }), ca);
      if (status === 200 && body.trim()) return body.trim();
      last = new Error(`exchange refused (${status}): ${body.slice(0, 300)}`);
      if (status < 500) break; // 4xx: wrong job or identity; retrying cannot help.
    } catch (error) { last = error; }
    await new Promise((r) => setTimeout(r, attempt * 5000));
  }
  throw last;
}

async function jwtSvid(audience, socket = '/run/spire/agent-sockets/spire-agent.sock') {
  socket = socket.replace(/^unix:\/\//, '');
  if (fs.existsSync(socket)) return viaAgent(audience, socket);
  if (process.env.ACTIONS_ID_TOKEN_REQUEST_URL || fs.existsSync(EXCHANGE.psatPath)) return viaExchange(audience);
  throw new Error(`no SPIRE Workload API at ${socket} and no GitHub OIDC token`);
}

module.exports = { jwtSvid };
