import { PING_TIMEOUT_S, RECONNECT_DELAYS_S } from "../config";
import { notifySignedOut, refreshSession } from "./http";
import type { ServerMessage } from "./types";

const UNAUTHORISED = 4401;

/**
 * Keep a socket to /ws open, reconnecting with backoff, until the returned function is called. Messages
 * sent while it was down are lost, but the server starts every connection with a fresh snapshot.
 */
export function openSocket(onMessage: (message: ServerMessage) => void): () => void {
  const url = `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws`;
  let socket: WebSocket | null = null;
  let attempt = 0;
  let stopped = false;
  let retryTimer: ReturnType<typeof setTimeout> | undefined;
  let deadTimer: ReturnType<typeof setTimeout> | undefined;

  function drop(ws: WebSocket) {
    ws.onmessage = null;
    ws.onclose = null;
    ws.close();
    clearTimeout(deadTimer);
    socket = null;
  }

  function reconnectLater() {
    const delay = RECONNECT_DELAYS_S[Math.min(attempt, RECONNECT_DELAYS_S.length - 1)] ?? 10;
    attempt += 1;
    retryTimer = setTimeout(connect, delay * 1000);
  }

  // A socket whose peer vanished can stay "open" for minutes; silence is the only reliable sign.
  function expectPing(ws: WebSocket) {
    clearTimeout(deadTimer);
    deadTimer = setTimeout(() => {
      drop(ws);
      reconnectLater();
    }, PING_TIMEOUT_S * 1000);
  }

  function connect() {
    if (stopped) return;
    const ws = new WebSocket(url);
    socket = ws;
    expectPing(ws);
    ws.onmessage = (event: MessageEvent<string>) => {
      attempt = 0;
      expectPing(ws);
      let message: ServerMessage;
      try {
        message = JSON.parse(event.data) as ServerMessage;
      } catch {
        return;
      }
      onMessage(message);
    };
    ws.onclose = (event) => {
      clearTimeout(deadTimer);
      socket = null;
      if (event.code !== UNAUTHORISED) {
        reconnectLater();
        return;
      }
      void refreshSession().then((ok) => {
        if (stopped) return;
        if (ok) reconnectLater();
        else notifySignedOut();
      });
    };
  }

  connect();
  return () => {
    stopped = true;
    clearTimeout(retryTimer);
    if (socket) drop(socket);
  };
}
