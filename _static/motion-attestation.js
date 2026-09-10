// SPDX-License-Identifier: 0BSD

import { createCollector } from './vendor/motion-attestation/collector.js';

const loader = document.getElementById('motion-attestation-loader');
const appName = loader?.dataset.app;
const requestTimeoutMs = 4_000;

if (appName) {
    uproot.onStart(() => start(appName));
}

async function requestJson(app, action, body) {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), requestTimeoutMs);

    try {
        const response = await uproot.api2(app, {
            body,
            headers: {
                'Content-Type': 'application/json',
                'X-Motion-Attestation-Action': action,
            },
            method: 'POST',
            signal: controller.signal,
        });
        return await response.json();
    } finally {
        window.clearTimeout(timer);
    }
}

function bindInteractiveElements(collector) {
    const selector = 'a[href], button, input:not([type="hidden"]), select, textarea';
    document.querySelectorAll(selector).forEach((element, index) => {
        const type = element.getAttribute('type') || element.tagName.toLowerCase();
        collector.bind(element, `${type}-${index}`);
    });
}

function start(app) {
    const form = document.getElementById('uproot-form');
    if (!form) return;

    const collector = createCollector();
    collector.attach();
    bindInteractiveElements(collector);

    let challenge = null;
    let challengePromise = null;

    async function initializeChallenge() {
        const result = await requestJson(app, 'init');
        if (typeof result.challengeId !== 'string' || !Number.isFinite(result.ttl)) {
            throw new Error(result.error || 'Could not initialize motion attestation');
        }

        challenge = {
            expiresAt: Date.now() + result.ttl,
            id: result.challengeId,
        };
        return challenge;
    }

    function currentChallenge() {
        if (challenge && challenge.expiresAt - Date.now() > 1_000) {
            return Promise.resolve(challenge);
        }
        if (!challengePromise) {
            challengePromise = initializeChallenge().finally(() => {
                challengePromise = null;
            });
        }
        return challengePromise;
    }

    challengePromise = initializeChallenge().finally(() => {
        challengePromise = null;
    });
    challengePromise.catch(() => {});

    async function attest() {
        const data = collector.getData();
        collector.detach();

        for (let attempt = 0; attempt < 2; attempt += 1) {
            const activeChallenge = await currentChallenge();
            const result = await requestJson(
                app,
                'verify',
                JSON.stringify({
                    cid: activeChallenge.id,
                    d: data,
                    ts: Date.now(),
                })
            );
            if (result.error !== 'Invalid or expired challenge') return;
            challenge = null;
        }
    }

    async function attestThenSubmit(targetForm) {
        try {
            await attest();
        } catch (error) {
            console.warn('Motion attestation unavailable; continuing submission.', error);
        }

        HTMLFormElement.prototype.submit.call(targetForm);
    }

    function beginAttestedSubmit(targetForm) {
        if (!uproot.beginSubmit(targetForm)) return false;
        void attestThenSubmit(targetForm);
        return true;
    }

    form.onsubmit = () => {
        beginAttestedSubmit(form);
        return false;
    };

    const submitForm = uproot.submitForm.bind(uproot);
    uproot.submitForm = (targetForm) => {
        if (targetForm !== form) return submitForm(targetForm);
        return beginAttestedSubmit(targetForm);
    };
}
