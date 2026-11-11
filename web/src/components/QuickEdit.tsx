import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import type { LocatedText } from "../api";
import type { PageBox } from "./PdfViewer";

export type QuickEditState = {
  // Page coordinates (scroll included) just below the clicked line.
  x: number;
  y: number;
  status: "locating" | "ready" | "saving";
  hit?: LocatedText;
  value: string;
  error?: string;
  // Where the clicked line sits, to mark it while the change waits.
  page?: number;
  box?: PageBox;
  queued?: boolean; // this run already has a queued change, shown in `value`
};

type Props = {
  state: QuickEditState;
  onChange: (value: string) => void;
  onSave: () => void;
  onClose: () => void;
};

const WIDTH = 460;

// Small editor anchored under text clicked on the preview. It shows the words
// as on the slide (LaTeX only when the sentence has math or other commands);
// saving queues the change, applied with the others in one compile.
export default function QuickEdit({ state, onChange, onSave, onClose }: Props) {
  const boxRef = useRef<HTMLDivElement>(null);
  const areaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (state.status === "ready" && state.hit) {
      const area = areaRef.current;
      area?.focus();
      area?.setSelectionRange(area.value.length, area.value.length);
    }
  }, [state.status, state.hit]);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    function onDown(e: MouseEvent) {
      const t = e.target as HTMLElement;
      // Clicking other text re-targets the editor; anything else closes it.
      if (boxRef.current?.contains(t) || t.closest(".deck-editable .textLayer")) return;
      onClose();
    }
    window.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onDown);
    return () => {
      window.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onDown);
    };
  }, [onClose]);

  const left = Math.max(8, Math.min(state.x, window.scrollX + window.innerWidth - WIDTH - 16));
  const hit = state.hit;
  const changed = hit !== undefined && (state.value !== hit.text || Boolean(state.queued));

  return createPortal(
    <div ref={boxRef} role="dialog" aria-label="Edit text"
      className="absolute z-50 border border-[var(--color-border)] bg-white p-3 shadow-lg"
      style={{ left, top: state.y + 8, width: WIDTH }}>
      {state.status === "locating" && <p className="text-sm text-slate-500">Finding this text in the source…</p>}
      {hit && (
        <>
          <label htmlFor="quick-edit" className="text-xs text-slate-500">
            {hit.raw ? `LaTeX source, line ${hit.line}` : "Text on the slide"}
          </label>
          <textarea id="quick-edit" ref={areaRef} rows={3} value={state.value}
            disabled={state.status === "saving"}
            onChange={(e) => onChange(e.target.value)}
            onKeyDown={(e) => {
              // Enter saves; Shift+Enter starts a new line on the slide.
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                if (changed) onSave();
              }
            }}
            className={`input-field mt-1 w-full text-sm ${hit.raw ? "font-mono" : ""}`} />
        </>
      )}
      {state.error && (
        <p role="alert" className="mt-2 text-sm text-red-700">{state.error}</p>
      )}
      <div className="mt-2 flex items-center justify-between gap-2">
        <p className="text-xs text-slate-500">
          {hit ? "Enter adds it to your changes; nothing recompiles until you apply them." : ""}
        </p>
        <div className="flex shrink-0 gap-2">
          <button type="button" className="btn-secondary px-3 py-1 text-sm" onClick={onClose}>Cancel</button>
          {hit && (
            <button type="button" className="btn-primary px-3 py-1 text-sm"
              disabled={!changed || state.status === "saving"} onClick={onSave}>
              {state.queued ? "Update change" : "Add change"}
            </button>
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}
