// SPDX-License-Identifier: 0BSD

import { createHash } from 'node:crypto';
import { copyFile, mkdir, readFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const projectRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const source = join(
    projectRoot,
    'node_modules',
    'motion-attestation',
    'src',
    'collector.js'
);
const destinationDirectory = join(
    projectRoot,
    '_static',
    'vendor',
    'motion-attestation'
);
const destination = join(destinationDirectory, 'collector.js');
const expectedSha256 = '50dd6c2b935fe8a338275c32b4868d190b65519119c340bb1755fee2342f2bc6';

const contents = await readFile(source);
const actualSha256 = createHash('sha256').update(contents).digest('hex');

if (actualSha256 !== expectedSha256) {
    throw new Error(`Unexpected motion-attestation collector digest: ${actualSha256}`);
}

await mkdir(destinationDirectory, { recursive: true });
await copyFile(source, destination);
