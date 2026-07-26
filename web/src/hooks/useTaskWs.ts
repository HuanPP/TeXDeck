import { useEffect, useRef } from "react";
import type { TaskEvent } from "../types";

const MAX_BACKOFF_MS = 30_000;

/**
 * Connect to /ws/generations/{id}. The server re-sends the current task state
 * on connect and closes the socket after a terminal status, so reconnecting is
 * safe and cheap. While enabled and no terminal event has arrived, dropped
 * connections are reopened with capped exponential backoff; once a terminal
 * event (succeeded/failed) arrives the hook stops reconnecting for good.
 * onReconnect fires after a successful reconnect so callers can resync state
 * they may have missed while offline.
 */
export function useTaskWs(
  genId: string | undefined,
  enabled: boolean,
  onEvent: (event: TaskEvent) => void,
  onReconnect?: () => void,
): void {
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;
  const onReconnectRef = useRef(onReconnect);
  onReconnectRef.current = onReconnect;

  useEffect(() => {
    if (!genId || !enabled) return;
    let ws: WebSocket | null = null;
    let retryTimer = 0;
    let attempt = 0;
    let sawTerminal = false;
    let disposed = false;

    const connect = () => {
      const protocol = window.location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${protocol}://${window.location.host}/ws/generations/${genId}`);
      ws.onopen = () => {
        if (attempt > 0) onReconnectRef.current?.();
        attempt = 0;
      };
      ws.onmessage = (msg) => {
        try {
          const event = JSON.parse(String(msg.data)) as TaskEvent;
          if (event.status === "succeeded" || event.status === "failed") sawTerminal = true;
          onEventRef.current(event);
        } catch (err) {
          console.error("Failed to parse WebSocket message", err);
        }
      };
      // No onerror handler synthesizing fake "failed" events: connection
      // problems are handled by onclose (which also fires after onerror).
      ws.onclose = () => {
        if (disposed || sawTerminal) return;
        attempt += 1;
        const delay = Math.min(MAX_BACKOFF_MS, 1000 * 2 ** Math.min(attempt - 1, 5));
        retryTimer = window.setTimeout(connect, delay);
      };
    };

    connect();
    return () => {
      disposed = true;
      window.clearTimeout(retryTimer);
      if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
        ws.close();
      }
    };
  }, [genId, enabled]);
}
