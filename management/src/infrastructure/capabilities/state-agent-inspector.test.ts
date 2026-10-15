// @vitest-environment node

import { createServer, type RequestListener, type Server } from 'node:http';
import { afterEach, describe, expect, it } from 'vitest';
import {
  inspectStateAgentHealth,
  StateAgentBadResponseError,
  StateAgentUnauthorizedError,
} from './state-agent-inspector';

const TOKEN = 'synthetic-state-agent-token';
const servers: Server[] = [];

afterEach(async () => {
  await Promise.all(
    servers
      .splice(0)
      .map(
        (server) =>
          new Promise<void>((resolve, reject) =>
            server.close((error) =>
              error === undefined ? resolve() : reject(error),
            ),
          ),
      ),
  );
});

describe('native State Agent inspection capability', () => {
  it('authenticates and returns only the bounded health projection', async () => {
    const url = await listen((request, response) => {
      expect(request.headers.authorization).toBe(`Bearer ${TOKEN}`);
      response.end(
        JSON.stringify({
          ok: true,
          agent: { name: 'agent', version: '1.2.3', uptime_ms: 42 },
          device: { root: '/sensitive-device-path' },
          state_decode: { ok: true, expected_size: 236, actual_size: 236 },
        }),
      );
    });

    const result = await inspectStateAgentHealth(url, TOKEN, 1);

    expect(result).toEqual({
      healthy: true,
      agent: { name: 'agent', version: '1.2.3', uptimeMs: 42 },
      stateDecode: { ok: true, expectedBytes: 236, actualBytes: 236 },
    });
    expect(JSON.stringify(result)).not.toContain('sensitive');
  });

  it('distinguishes a verified unhealthy observation from capability failure', async () => {
    const url = await listen((_request, response) => {
      response.end(
        JSON.stringify({
          ok: false,
          agent: { name: 'agent', version: '1.2.3', uptime_ms: 42 },
          state_decode: { ok: false, expected_size: 236, actual_size: 100 },
        }),
      );
    });
    await expect(inspectStateAgentHealth(url, TOKEN, 1)).resolves.toMatchObject(
      {
        healthy: false,
        stateDecode: { ok: false },
      },
    );
  });

  it('classifies authentication and contradictory response failures', async () => {
    const unauthorized = await listen((_request, response) => {
      response.writeHead(403).end();
    });
    await expect(
      inspectStateAgentHealth(unauthorized, TOKEN, 1),
    ).rejects.toBeInstanceOf(StateAgentUnauthorizedError);

    const malformed = await listen((_request, response) => {
      response.end(
        JSON.stringify({
          ok: true,
          agent: { name: 'agent', version: '1', uptime_ms: 1 },
          state_decode: { ok: false, expected_size: 1, actual_size: 1 },
        }),
      );
    });
    await expect(
      inspectStateAgentHealth(malformed, TOKEN, 1),
    ).rejects.toBeInstanceOf(StateAgentBadResponseError);
  });

  it('rejects an oversized response without exposing its content', async () => {
    const url = await listen((_request, response) => {
      response.end('x'.repeat(65 * 1024));
    });
    await expect(inspectStateAgentHealth(url, TOKEN, 1)).rejects.toThrow(
      'exceeds the size limit',
    );
  });
});

async function listen(handler: RequestListener) {
  const server = createServer(handler);
  servers.push(server);
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  if (address === null || typeof address === 'string') {
    throw new Error('server did not bind');
  }
  return `http://127.0.0.1:${address.port}`;
}
