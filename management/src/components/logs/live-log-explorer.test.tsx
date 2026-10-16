import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { compileSafeLogRegex, LiveLogExplorer } from './live-log-explorer';

const sockets: FakeWebSocket[] = [];

afterEach(() => {
  cleanup();
  sockets.splice(0);
  vi.unstubAllGlobals();
});

describe('LiveLogExplorer', () => {
  it('keeps capturing while display is paused and catches up on resume', async () => {
    vi.stubGlobal('WebSocket', FakeWebSocket);
    render(<LiveLogExplorer />);
    fireEvent.click(screen.getByRole('button', { name: 'Start capture' }));
    const socket = sockets[0] as FakeWebSocket;
    socket.deliver(recordMessage(1, 'first line'));
    expect(await screen.findByText('first line')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Pause view' }));
    socket.deliver(recordMessage(2, 'captured while paused'));
    expect(screen.queryByText('captured while paused')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Resume view' }));
    expect(
      await screen.findByText('captured while paused'),
    ).toBeInTheDocument();
  });

  it('keeps a bounded frozen snapshot stable while live retention advances', async () => {
    vi.stubGlobal('WebSocket', FakeWebSocket);
    render(<LiveLogExplorer />);
    fireEvent.click(screen.getByRole('button', { name: 'Start capture' }));
    const socket = sockets[0] as FakeWebSocket;
    socket.deliver(recordMessage(1, 'frozen line'));
    expect(await screen.findByText('frozen line')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Pause view' }));
    for (let sequence = 2; sequence <= 2_010; sequence += 1) {
      socket.deliver(recordMessage(sequence, `new line ${sequence}`));
    }
    expect(screen.getByText('frozen line')).toBeInTheDocument();
    expect(screen.queryByText('new line 2010')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Resume view' }));
    expect(await screen.findByText('new line 2010')).toBeInTheDocument();
  });

  it('changes browser retention without recreating the source subscription', () => {
    vi.stubGlobal('WebSocket', FakeWebSocket);
    render(<LiveLogExplorer />);
    fireEvent.click(screen.getByRole('button', { name: 'Start capture' }));
    fireEvent.change(
      screen.getByRole('combobox', { name: 'Retained line count' }),
      {
        target: { value: '1000' },
      },
    );
    expect(sockets).toHaveLength(1);
  });

  it('rejects regex constructs with credible catastrophic backtracking risk', () => {
    expect(compileSafeLogRegex('(a+)+$').error).toMatch(/repeated-group/);
    expect(compileSafeLogRegex('(a|aa)+$').error).toMatch(/repeated-group/);
    expect(compileSafeLogRegex('a+a+$').error).toMatch(/unbounded/);
    expect(compileSafeLogRegex('error.*timeout').expression).toBeInstanceOf(
      RegExp,
    );
  });

  it('bounds rendered records while retaining all matches for investigation', async () => {
    vi.stubGlobal('WebSocket', FakeWebSocket);
    render(<LiveLogExplorer />);
    fireEvent.click(screen.getByRole('button', { name: 'Start capture' }));
    const socket = sockets[0] as FakeWebSocket;
    socket.deliver(
      recordBatch(
        Array.from({ length: 600 }, (_, index) => ({
          sequence: index + 1,
          message: `volume line ${index + 1}`,
        })),
      ),
    );

    expect(
      await screen.findByText(/Showing the latest 500 of 600 matching records/),
    ).toBeInTheDocument();
    expect(screen.queryByText('volume line 1')).not.toBeInTheDocument();
    expect(screen.getByText('volume line 600')).toBeInTheDocument();
  });

  it('counts each retention eviction exactly once', async () => {
    vi.stubGlobal('WebSocket', FakeWebSocket);
    render(<LiveLogExplorer />);
    fireEvent.click(screen.getByRole('button', { name: 'Start capture' }));
    const socket = sockets[0] as FakeWebSocket;
    socket.deliver(
      recordBatch(
        Array.from({ length: 2_100 }, (_, index) => ({
          sequence: index + 1,
          message: `retention line ${index + 1}`,
        })),
      ),
    );

    expect(
      await screen.findByText(
        '0 transport records dropped; 100 records truncated by browser retention.',
      ),
    ).toBeInTheDocument();
  });
});

class FakeWebSocket extends EventTarget {
  public constructor(public readonly url: string) {
    super();
    sockets.push(this);
  }

  public close(): void {
    this.dispatchEvent(new Event('close'));
  }

  public deliver(value: unknown): void {
    this.dispatchEvent(
      new MessageEvent('message', { data: JSON.stringify(value) }),
    );
  }
}

function recordMessage(sequence: number, message: string) {
  return recordBatch([{ sequence, message }]);
}

function recordBatch(
  values: readonly { readonly sequence: number; readonly message: string }[],
) {
  return {
    type: 'logs.records',
    dropped: 0,
    records: values.map(({ sequence, message }) => ({
      sequence,
      source: 'addon-runtime',
      severity: 'info',
      receivedAt: new Date(
        Date.parse('2026-10-15T02:00:00.000Z') + sequence * 1_000,
      ).toISOString(),
      message,
    })),
  };
}
