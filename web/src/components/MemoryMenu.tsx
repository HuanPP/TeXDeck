import { useEffect, useRef, useState } from "react";
import { ChevronDown } from "lucide-react";
import { getMemory, saveMemory, type Memory } from "../api";

const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));
const toText = (lines: string[]) => lines.join("\n");
const toLines = (text: string) => text.split("\n").map((l) => l.trim()).filter(Boolean);

// What the model has learned from this user's edits. It writes here on its
// own; every new deck and edit reads it; the user can rewrite it freely.
export default function MemoryMenu() {
  const [open, setOpen] = useState(false);
  const [saved, setSaved] = useState<Memory | null>(null);
  const [habits, setHabits] = useState("");
  const [mistakes, setMistakes] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    // Reload on every open: edits since the last one may have added lines.
    getMemory().then((m) => {
      setSaved(m);
      setHabits(toText(m.habits));
      setMistakes(toText(m.mistakes));
    }, (e) => setError(errMsg(e)));
    const onDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const dirty = saved !== null
    && (toText(toLines(habits)) !== toText(saved.habits) || toText(toLines(mistakes)) !== toText(saved.mistakes));

  async function save() {
    setError(null);
    setBusy(true);
    try {
      const m = await saveMemory({ habits: toLines(habits), mistakes: toLines(mistakes) });
      setSaved(m);
      setHabits(toText(m.habits));
      setMistakes(toText(m.mistakes));
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div ref={rootRef} className="relative text-sm">
      <button type="button" className="btn-secondary px-3 py-1 text-xs" aria-expanded={open}
        onClick={() => setOpen((o) => !o)}>
        Memory
        <ChevronDown size={14} />
      </button>
      {open && (
        <div className="absolute right-0 top-full z-20 mt-2 w-[26rem] space-y-3 rounded-md border border-[var(--color-border)] bg-[var(--color-surface-container)] p-4 shadow-lg">
          <p className="text-xs leading-relaxed text-slate-500">
            The model notes your habits and the mistakes you had to fix while you edit, and follows them in
            every new deck and edit. One per line; change or delete anything.
          </p>
          <label className="block text-xs text-slate-600">
            Your habits
            <textarea className="input-field mt-1 min-h-24 text-xs" value={habits} disabled={saved === null}
              placeholder="e.g. At most four bullets per slide"
              onChange={(e) => setHabits(e.target.value)} />
          </label>
          <label className="block text-xs text-slate-600">
            Mistakes to avoid
            <textarea className="input-field mt-1 min-h-24 text-xs" value={mistakes} disabled={saved === null}
              placeholder="e.g. Chart labels must not overlap"
              onChange={(e) => setMistakes(e.target.value)} />
          </label>
          {error && <p role="alert" className="text-xs text-red-600">{error}</p>}
          <div className="flex justify-end">
            <button type="button" className="btn-primary px-3 py-1 text-xs" disabled={!dirty || busy}
              onClick={() => void save()}>
              {busy ? "Saving…" : "Save"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
