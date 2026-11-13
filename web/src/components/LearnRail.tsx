import { useEffect, useRef, useState } from "react";
import { Check, ChevronLeft, ChevronRight, Volume2, VolumeX } from "lucide-react";
import { getProgress, saveProgress, type SourceAnchor } from "../api";
import { playDone, playFinish, setSoundMuted, soundMuted } from "../sounds";

// The notebook's progress rail, left of the source: one dot per part of the
// notes (each \notehead, or a sheet without any), in reading order. Marking a
// part as learnt fills its dot. Collapsed it is a strip of dots; expanded it
// lists the parts, and a title jumps the notes there.

export type LearnUnit = { key: string; title: string; sheet: string; anchor: SourceAnchor; first: boolean };

// Parts from the source-map anchors (already in notes order): \notehead
// headings, grouped under their sheet; a sheet with none is one part itself.
export function learnUnits(anchors: SourceAnchor[]): LearnUnit[] {
  const units: LearnUnit[] = [];
  let sheet: SourceAnchor | null = null;
  let heads = 0;
  const closeSheet = () => {
    if (sheet && heads === 0) units.push({ key: `${sheet.title}`, title: sheet.title, sheet: "", anchor: sheet, first: true });
  };
  for (const a of anchors) {
    if (a.level === 0) {
      closeSheet();
      sheet = a;
      heads = 0;
    } else if (a.level === 1) {
      const sheetTitle = sheet?.title ?? "";
      units.push({ key: `${sheetTitle}\u0001${a.title}`, title: a.title, sheet: sheetTitle, anchor: a, first: heads === 0 });
      heads += 1;
    }
  }
  closeSheet();
  // Keep reading order: a childless sheet was pushed when the next sheet began.
  units.sort((x, y) => x.anchor.notes_page - y.anchor.notes_page || x.anchor.y - y.anchor.y);
  const seen = new Map<string, number>();
  for (const u of units) {
    const n = seen.get(u.key) ?? 0;
    seen.set(u.key, n + 1);
    if (n) u.key = `${u.key}\u0001${n + 1}`; // the same title twice in one sheet
  }
  return units;
}

type Props = {
  id: string;
  anchors: SourceAnchor[];
  notesPage: number; // where the notes are scrolled to: the part being read
  onGo: (anchor: SourceAnchor) => void;
  onDone?: (keys: Set<string>) => void; // the parts marked as learnt, whenever they change
};

export default function LearnRail({ id, anchors, notesPage, onGo, onDone }: Props) {
  const [open, setOpen] = useState(false);
  const [done, setDone] = useState<Set<string>>(new Set());
  const [popped, setPopped] = useState<string | null>(null);
  const [finished, setFinished] = useState(false);
  const [muted, setMuted] = useState(soundMuted);
  const loaded = useRef(false);
  const units = learnUnits(anchors);

  useEffect(() => {
    let cancelled = false;
    loaded.current = false;
    getProgress(id).then((keys) => {
      if (cancelled) return;
      setDone(new Set(keys));
      loaded.current = true;
    }, () => undefined);
    return () => { cancelled = true; };
  }, [id]);

  useEffect(() => { onDone?.(done); }, [done, onDone]);

  useEffect(() => {
    if (!finished) return;
    const t = window.setTimeout(() => setFinished(false), 4000);
    return () => window.clearTimeout(t);
  }, [finished]);

  if (!units.length) return null;

  const count = units.filter((u) => done.has(u.key)).length;
  const fraction = count / units.length;
  // The part being read: the last one starting at or before the visible page.
  const reading = units.reduce<string | null>((hit, u) => (u.anchor.notes_page <= notesPage ? u.key : hit), units[0].key);

  function toggle(u: LearnUnit) {
    if (!loaded.current) return;
    const next = new Set(done);
    if (next.has(u.key)) {
      next.delete(u.key);
    } else {
      next.add(u.key);
      setPopped(u.key);
      window.setTimeout(() => setPopped((p) => (p === u.key ? null : p)), 700);
      const all = units.every((x) => next.has(x.key));
      if (all) {
        playFinish();
        setFinished(true);
      } else {
        playDone();
      }
    }
    setDone(next);
    // Keys of parts no longer in the notes are kept: a regenerate may bring them back.
    void saveProgress(id, [...next]).catch(() => undefined);
  }

  const ring = (size: number) => {
    const r = size / 2 - 2.5;
    const c = 2 * Math.PI * r;
    return (
      <svg width={size} height={size} className="-rotate-90" aria-hidden="true">
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="#e2e8f0" strokeWidth="3" />
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="#10b981" strokeWidth="3" strokeLinecap="round"
          strokeDasharray={c} strokeDashoffset={c * (1 - fraction)} style={{ transition: "stroke-dashoffset 0.5s ease" }} />
      </svg>
    );
  };

  const dot = (u: LearnUnit, big: boolean) => {
    const isDone = done.has(u.key);
    const isReading = u.key === reading;
    const size = big ? "h-[18px] w-[18px]" : "h-3 w-3";
    return (
      <button type="button" onClick={() => toggle(u)} aria-pressed={isDone}
        aria-label={`${isDone ? "Mark as not learnt" : "Mark as learnt"}: ${u.title}`}
        title={big ? (isDone ? "Learnt (click to undo)" : "Mark as learnt") : `${u.title}${isDone ? " (learnt)" : ""}`}
        className={`relative inline-flex shrink-0 items-center justify-center rounded-full transition-colors ${size} ${isDone
          ? "bg-emerald-500 text-white"
          : `border-2 bg-white ${isReading ? "border-indigo-500" : "border-slate-300 hover:border-emerald-400"}`}
          ${popped === u.key ? "learn-pop" : ""}`}>
        {isDone && big && <Check className="h-3 w-3" strokeWidth={3} />}
        {popped === u.key && <span className="learn-ring pointer-events-none absolute inset-0 rounded-full border-2 border-emerald-400" />}
      </button>
    );
  };

  return (
    <nav aria-label="Learning progress"
      className={`relative mr-1.5 flex shrink-0 flex-col border border-[var(--color-border)] bg-white ${open ? "w-60" : "w-9"}`}>
      {open ? (
        <>
          <div className="shrink-0 space-y-2 border-b border-[var(--color-border)] px-3 py-2">
            <div className="flex items-center gap-2">
              <span className="flex-1 text-xs font-medium text-[var(--color-heading)]">Progress</span>
              <button type="button" className="p-0.5 text-slate-400 hover:text-slate-700"
                aria-label={muted ? "Turn sounds on" : "Turn sounds off"} title={muted ? "Sounds off" : "Sounds on"}
                onClick={() => { setSoundMuted(!muted); setMuted(!muted); }}>
                {muted ? <VolumeX className="h-3.5 w-3.5" /> : <Volume2 className="h-3.5 w-3.5" />}
              </button>
              <button type="button" className="p-0.5 text-slate-400 hover:text-slate-700" aria-label="Collapse progress"
                onClick={() => setOpen(false)}>
                <ChevronLeft className="h-4 w-4" />
              </button>
            </div>
            <div className="flex items-center gap-2">
              <div className="h-2 flex-1 overflow-hidden rounded-full bg-slate-100">
                <div className="h-full rounded-full bg-emerald-500" style={{ width: `${fraction * 100}%`, transition: "width 0.5s ease" }} />
              </div>
              <span className="font-mono text-[11px] text-slate-500">{count}/{units.length}</span>
            </div>
          </div>
          <ol className="min-h-0 flex-1 overflow-auto px-2 py-2 text-[12.5px]">
            {units.map((u) => (
              <li key={u.key}>
                {u.first && u.sheet && <p className="mt-2 truncate px-1 pb-1 text-[10.5px] uppercase tracking-wide text-slate-400 first:mt-0" title={u.sheet}>{u.sheet}</p>}
                <div className={`flex items-center gap-2 px-1 py-1 ${u.key === reading ? "bg-indigo-50" : ""}`}>
                  {dot(u, true)}
                  <button type="button" className={`min-w-0 flex-1 truncate text-left hover:text-indigo-700 ${done.has(u.key) ? "text-slate-400" : "text-slate-700"}`}
                    title={u.title} onClick={() => onGo(u.anchor)}>
                    {u.title}
                  </button>
                </div>
              </li>
            ))}
          </ol>
        </>
      ) : (
        <>
          <button type="button" className="flex shrink-0 flex-col items-center gap-0.5 border-b border-[var(--color-border)] py-1.5 text-slate-400 hover:text-slate-700"
            aria-label="Expand progress" title={`${count} of ${units.length} learnt: expand`} onClick={() => setOpen(true)}>
            {ring(24)}
            <ChevronRight className="h-3 w-3" />
          </button>
          <ol className="flex min-h-0 flex-1 flex-col items-center gap-2 overflow-auto py-2">
            {units.map((u) => (
              <li key={u.key} className={`flex ${u.first && u.sheet && u !== units[0] ? "mt-2" : ""}`}>{dot(u, false)}</li>
            ))}
          </ol>
        </>
      )}
      {finished && (
        <div role="status" className="learn-pop absolute left-full top-3 z-30 ml-2 whitespace-nowrap rounded-full bg-emerald-500 px-3 py-1.5 text-xs font-medium text-white shadow-lg">
          All {units.length} parts learnt
        </div>
      )}
    </nav>
  );
}

// Where each learnt part sits, to tint it: in the notes, from its heading to the
// next part's heading (or the next sheet's title); in the source, whole pages
// whose every citing part is learnt.
export function learntShades(anchors: SourceAnchor[], done: Set<string>, notesPages: number) {
  const units = learnUnits(anchors);
  const starts = anchors.filter((a) => a.level <= 1);
  const notes: { page: number; y0: number; y1: number; label?: string }[] = [];
  for (const u of units) {
    if (!done.has(u.key)) continue;
    const a = u.anchor;
    const at = starts.indexOf(a);
    const next = at >= 0 ? starts[at + 1] : undefined;
    const end = next ? { page: next.notes_page, y: Math.max(0, next.y - 0.008) } : { page: notesPages || a.notes_page, y: 1 };
    const y0 = Math.max(0, a.y - 0.008);
    for (let p = a.notes_page; p <= end.page; p += 1) {
      const top = p === a.notes_page ? y0 : 0;
      const bottom = p === end.page ? end.y : 1;
      if (bottom > top + 0.005) notes.push({ page: p, y0: top, y1: bottom, label: p === a.notes_page ? "Learnt" : undefined });
    }
  }
  const covered = new Map<string, boolean>(); // "doc:page" -> all citing parts learnt
  for (const u of units) {
    const a = u.anchor;
    for (let p = a.page; p <= a.to; p += 1) {
      const k = `${a.doc}:${p}`;
      covered.set(k, (covered.get(k) ?? true) && done.has(u.key));
    }
  }
  const source = (doc: number) => [...covered].filter(([k, ok]) => ok && k.startsWith(`${doc}:`))
    .map(([k]) => ({ page: Number(k.split(":")[1]), y0: 0, y1: 1, label: "Learnt" }));
  return { notes, source };
}
