import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";

export type QuickEditState = {
  // Page coordinates (scroll included) just below the clicked line.
  x: number;
  y: number;
  status: "locating" | "ready" | "saving";
  line?: number;
  source?: string;
  value: string;
  error?: string;
};

type Props = {
  state: QuickEditState;
  onChange: (value: string) => void;
  onSave: () => void;
  onClose: () => void;
};

const WIDTH = 460;

// Small editor anchored under text clicked on the preview. It edits the raw
// LaTeX source line behind that text; saving recompiles without the model.
export default function QuickEdit({ state, onChange, onSave, onClose }: Props) {
  const boxRef = useRef<HTMLDivElement>(null);
  const areaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (state.status === "ready" && state.source !== undefined) {
      const area = areaRef.current;
      area?.focus();
      area?.setSelectionRange(area.value.length, area.value.length);
    }
  }, [state.status, state.source]);

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
  const changed = state.source !== undefined && state.value !== state.source;

  return createPortal(
    <div ref={boxRef} role="dialog" aria-label="Edit text"
      className="absolute z-50 border border-[var(--color-border)] bg-white p-3 shadow-lg"
      style={{ left, top: state.y + 8, width: WIDTH }}>
      {state.status === "locating" && <p className="text-sm text-slate-500">Finding this text in the source…</p>}
      {state.source !== undefined && (
        <>
          <label htmlFor="quick-edit" className="text-xs text-slate-500">
            LaTeX source, line {state.line}
          </label>
          <textarea id="quick-edit" ref={areaRef} rows={3} value={state.value}
            disabled={state.status === "saving"}
            onChange={(e) => onChange(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && changed) {
                e.preventDefault();
                onSave();
              }
            }}
            className="input-field mt-1 w-full font-mono text-sm" />
        </>
      )}
      {state.error && (
        <p role="alert" className="mt-2 text-sm text-red-700">{state.error}</p>
      )}
      <div className="mt-2 flex items-center justify-between gap-2">
        <p className="text-xs text-slate-500">
          {state.source !== undefined ? "Saves this line and recompiles; no model call." : ""}
        </p>
        <div className="flex shrink-0 gap-2">
          <button type="button" className="btn-secondary px-3 py-1 text-sm" onClick={onClose}>Cancel</button>
          {state.source !== undefined && (
            <button type="button" className="btn-primary px-3 py-1 text-sm"
              disabled={!changed || state.status === "saving"} onClick={onSave}>
              {state.status === "saving" ? "Saving…" : "Save"}
            </button>
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}
