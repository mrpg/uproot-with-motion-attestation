// SPDX-License-Identifier: 0BSD

import { timingSafeEqual } from 'node:crypto';
import { createServer as createHttpServer } from 'node:http';
import { createServer as createAttestationServer } from 'motion-attestation';

function numberSetting(name, fallback) {
    const value = Number(process.env[name] ?? fallback);
    if (!Number.isFinite(value)) {
        throw new Error(`${name} must be a number`);
    }
    return value;
}

const port = numberSetting('MOTION_ATTESTATION_PORT', 35001);
const scoreThreshold = numberSetting('MOTION_ATTESTATION_SCORE_THRESHOLD', 0.5);
const challengeTtl = numberSetting('MOTION_ATTESTATION_CHALLENGE_TTL_MS', 60_000);
const proxyKey = process.env.MOTION_ATTESTATION_PROXY_KEY;

if (!Number.isInteger(port) || port < 1 || port > 65_535) {
    throw new Error('MOTION_ATTESTATION_PORT must be an integer from 1 to 65535');
}
if (scoreThreshold < 0 || scoreThreshold > 1) {
    throw new Error('MOTION_ATTESTATION_SCORE_THRESHOLD must be between 0 and 1');
}
if (!Number.isInteger(challengeTtl) || challengeTtl < 1_000) {
    throw new Error('MOTION_ATTESTATION_CHALLENGE_TTL_MS must be an integer of at least 1000');
}
if (!proxyKey) {
    throw new Error('MOTION_ATTESTATION_PROXY_KEY is required');
}

const attestation = createAttestationServer({
    challengeTtl,
    scoreThreshold,
});
const attestationHandler = attestation.handler();

function authorized(requestKey) {
    if (typeof requestKey !== 'string') return false;
    const actual = Buffer.from(requestKey);
    const expected = Buffer.from(proxyKey);
    return actual.length === expected.length && timingSafeEqual(actual, expected);
}

const server = createHttpServer((request, response) => {
    if (!authorized(request.headers['x-motion-proxy-key'])) {
        response.writeHead(403, {
            'Cache-Control': 'no-store',
            'Content-Type': 'application/json',
        });
        response.end(JSON.stringify({ error: 'Forbidden' }));
        return;
    }

    if (request.url === '/health' && request.method === 'GET') {
        response.writeHead(200, {
            'Cache-Control': 'no-store',
            'Content-Type': 'application/json',
        });
        response.end(JSON.stringify({ ok: true }));
        return;
    }

    attestationHandler(request, response);
});

server.on('clientError', (clientError, socket) => {
    void clientError;
    socket.end('HTTP/1.1 400 Bad Request\r\n\r\n');
});

server.listen(port, '127.0.0.1', () => {
    console.log(`motion-attestation listening on 127.0.0.1:${port}`);
});

function close() {
    server.close(() => process.exit(0));
}

process.on('SIGINT', close);
process.on('SIGTERM', close);
