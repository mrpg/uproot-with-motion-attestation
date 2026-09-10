// SPDX-License-Identifier: 0BSD

const baseUrl = process.env.MOTION_ATTESTATION_URL;
const proxyKey = process.env.MOTION_ATTESTATION_PROXY_KEY;

if (!baseUrl || !proxyKey) {
    throw new Error('Motion-attestation connection settings are missing');
}

for (let attempt = 0; attempt < 50; attempt += 1) {
    try {
        const response = await fetch(`${baseUrl}/health`, {
            headers: { 'X-Motion-Proxy-Key': proxyKey },
        });
        if (response.ok) process.exit(0);
    } catch {
        // The sidecar may still be starting.
    }

    await new Promise((resolve) => setTimeout(resolve, 100));
}

throw new Error('motion-attestation did not become ready');
