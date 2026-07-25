import { useEffect, useRef } from "react";
import type { TaskEvent } from "../types";

/** Connect to /ws/generations/{id}; server closes after terminal status. */
export function useTaskWs(
  genId: string | undefined,
  enabled: boolean,
  onEvent: (event: TaskEvent) => void,
): void {
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;

  useEffect(() => {
    if (!genId || !enabled) return;
    const protocol = window.location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${protocol}://${window.location.host}/ws/generations/${genId}`);
    ws.onmessage = (msg) => {
      try {
        onEventRef.current(JSON.parse(String(msg.data)) as TaskEvent);
      } catch (err) {
        console.error("Failed to parse WebSocket message", err);
      }
    };
    ws.onerror = () => {
      onEventRef.current({
        gen_id: genId, status: "failed", percent: 0, stage: "", error: "WebSocket connection failed",
      });
    };
    return () => {
      if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) ws.close();
    };
  }, [genId, enabled]);
}
