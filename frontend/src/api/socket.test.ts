import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { openSocket } from "./socket";

const http = vi.hoisted(() => ({ refreshSession: vi.fn(), notifySignedOut: vi.fn() }));
vi.mock("./http", () => http);

class FakeSocket {
  static opened: FakeSocket[] = [];
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  closed = false;

  constructor(readonly url: string) {
    FakeSocket.opened.push(this);
  }

  close() {
    this.closed = true;
  }

  receive(message: unknown) {
    this.onmessage?.({ data: JSON.stringify(message) });
  }

  drop(code = 1006) {
    this.onclose?.({ code });
  }
}

const latest = () => FakeSocket.opened[FakeSocket.opened.length - 1]!;

beforeEach(() => {
  FakeSocket.opened = [];
  vi.useFakeTimers();
  vi.stubGlobal("WebSocket", FakeSocket);
  http.refreshSession.mockReset();
  http.notifySignedOut.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("openSocket", () => {
  it("passes each message on", () => {
    const onMessage = vi.fn();
    openSocket(onMessage);
    latest().receive({ type: "ping" });
    expect(latest().url).toMatch(/^ws:\/\/.+\/ws$/);
    expect(onMessage).toHaveBeenCalledWith({ type: "ping" });
  });

  it("reconnects after 1, 2, 5, 10 and then every 10 seconds", () => {
    openSocket(() => undefined);
    for (const seconds of [1, 2, 5, 10, 10]) {
      const count = FakeSocket.opened.length;
      latest().drop();
      vi.advanceTimersByTime(seconds * 1000 - 1);
      expect(FakeSocket.opened).toHaveLength(count);
      vi.advanceTimersByTime(1);
      expect(FakeSocket.opened).toHaveLength(count + 1);
    }
  });

  it("starts the backoff over once a socket hears from the server", () => {
    openSocket(() => undefined);
    latest().drop();
    vi.advanceTimersByTime(1000);
    latest().drop();
    vi.advanceTimersByTime(2000);
    latest().receive({ type: "ping" });
    latest().drop();
    vi.advanceTimersByTime(1000);
    expect(FakeSocket.opened).toHaveLength(4);
  });

  it("treats a minute without a ping as a dead socket", () => {
    openSocket(() => undefined);
    const first = latest();
    vi.advanceTimersByTime(50_000);
    first.receive({ type: "ping" });
    vi.advanceTimersByTime(59_999);
    expect(first.closed).toBe(false);
    vi.advanceTimersByTime(1);
    expect(first.closed).toBe(true);
    vi.advanceTimersByTime(1000);
    expect(FakeSocket.opened).toHaveLength(2);
  });

  it("refreshes the session before reconnecting after 4401", async () => {
    http.refreshSession.mockResolvedValue(true);
    openSocket(() => undefined);
    latest().drop(4401);
    await vi.advanceTimersByTimeAsync(1000);
    expect(http.refreshSession).toHaveBeenCalledOnce();
    expect(FakeSocket.opened).toHaveLength(2);
  });

  it("signs the user out when the refresh after 4401 is refused", async () => {
    http.refreshSession.mockResolvedValue(false);
    openSocket(() => undefined);
    latest().drop(4401);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(http.notifySignedOut).toHaveBeenCalledOnce();
    expect(FakeSocket.opened).toHaveLength(1);
  });

  it("keeps trying when the refresh after 4401 gets no answer", async () => {
    http.refreshSession.mockRejectedValue(new Error("offline"));
    openSocket(() => undefined);
    latest().drop(4401);
    await vi.advanceTimersByTimeAsync(1000);
    expect(http.notifySignedOut).not.toHaveBeenCalled();
    expect(FakeSocket.opened).toHaveLength(2);
  });

  it("stops for good when closed", () => {
    const close = openSocket(() => undefined);
    const socket = latest();
    close();
    expect(socket.closed).toBe(true);
    vi.advanceTimersByTime(120_000);
    expect(FakeSocket.opened).toHaveLength(1);
  });
});
