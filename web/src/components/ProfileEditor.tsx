import { ArrowDown, ArrowUp, Plus, Trash2, X } from "lucide-react";
import type { BlueprintSection, Profile, QuestionType, Topic } from "../api";
import { renumber } from "../examNumbering";
import { TYPE_LABELS } from "../pages/ExamList";

// An exam's profile, editable: its structure (sections), its topics and their
// share of the marks, how it asks and how it marks.

const TYPES = Object.keys(TYPE_LABELS) as QuestionType[];
const sum = (xs: number[]) => xs.reduce((a, b) => a + (Number(b) || 0), 0);

function TopicEditor({ topics, onChange }: { topics: Topic[]; onChange: (t: Topic[]) => void }) {
  const set = (i: number, patch: Partial<Topic>) => onChange(topics.map((t, j) => (j === i ? { ...t, ...patch } : t)));
  const total = sum(topics.map((t) => t.weight));
  return (
    <div>
      <div className="mb-1.5 flex items-baseline justify-between">
        <span className="text-sm text-slate-600">Topics and their share of the marks</span>
        <span className={`text-xs ${Math.abs(total - 100) > 1 && topics.length ? "text-amber-700" : "text-slate-500"}`}>{total}%</span>
      </div>
      <ul className="space-y-1">
        {topics.map((t, i) => (
          <li key={i} className="flex items-center gap-2 text-sm">
            <input className="input-field min-w-0 flex-1 px-2 py-1 text-sm" aria-label="Topic" value={t.name}
              onChange={(e) => set(i, { name: e.target.value })} />
            <input className="input-field w-16 px-1 py-1 text-center text-sm" type="number" min={0} max={100} aria-label="Share in percent"
              value={t.weight} onChange={(e) => set(i, { weight: Number(e.target.value) || 0 })} />
            <span className="text-xs text-slate-400">%</span>
            <input className="input-field hidden min-w-0 flex-[2] px-2 py-1 text-xs sm:block" aria-label="How it is asked" placeholder="How it is asked"
              value={t.note} onChange={(e) => set(i, { note: e.target.value })} />
            <button type="button" className="p-1 text-slate-400 hover:text-red-600" aria-label="Remove topic"
              onClick={() => onChange(topics.filter((_, j) => j !== i))}><X className="h-3.5 w-3.5" /></button>
          </li>
        ))}
      </ul>
      <button type="button" className="mt-1.5 inline-flex items-center gap-1 text-xs text-[var(--color-primary)] hover:underline"
        onClick={() => onChange([...topics, { name: "", weight: 0, note: "" }])}>
        <Plus className="h-3.5 w-3.5" /> Add a topic
      </button>
    </div>
  );
}

export default function ProfileEditor({ value, onChange }: { value: Profile; onChange: (p: Profile) => void }) {
  const set = (patch: Partial<Profile>) => onChange({ ...value, ...patch });
  const setSection = (i: number, patch: Partial<BlueprintSection>) =>
    set({ sections: value.sections.map((s, j) => (j === i ? { ...s, ...patch } : s)) });
  const move = (i: number, d: number) => {
    const s = [...value.sections];
    [s[i], s[i + d]] = [s[i + d], s[i]];
    set({ sections: renumber(s) });
  };
  const resize = (s: BlueprintSection, count: number): Partial<BlueprintSection> => ({
    count,
    points: Array.from({ length: count }, (_, k) => s.points[k] ?? s.points.at(-1) ?? 1),
    parts: Array.from({ length: count }, (_, k) => s.parts[k] ?? ""),
  });
  const total = sum(value.sections.flatMap((s) => s.points));
  const questions = value.sections.reduce((n, s) => n + s.count, 0);

  return (
    <div className="space-y-5">
      <div className="grid gap-4 sm:grid-cols-[1fr_auto]">
        <label className="text-sm text-slate-600">Title
          <input className="input-field mt-1" value={value.title} onChange={(e) => set({ title: e.target.value })} />
        </label>
        <label className="text-sm text-slate-600">Minutes
          <input className="input-field mt-1 w-24" type="number" min={0} value={value.duration || ""}
            onChange={(e) => set({ duration: Number(e.target.value) || 0 })} />
        </label>
      </div>
      <label className="block text-sm text-slate-600">Notes to candidates
        <textarea className="input-field mt-1 min-h-12 text-sm" value={value.instructions} onChange={(e) => set({ instructions: e.target.value })} />
      </label>
      <TopicEditor topics={value.topics} onChange={(topics) => set({ topics })} />
      <label className="block text-sm text-slate-600">How it asks
        <textarea className="input-field mt-1 min-h-16 text-sm" value={value.style} onChange={(e) => set({ style: e.target.value })} />
      </label>
      <label className="block text-sm text-slate-600">How it marks
        <textarea className="input-field mt-1 min-h-16 text-sm" value={value.marking} onChange={(e) => set({ marking: e.target.value })} />
      </label>
      <div className="flex items-baseline justify-between">
        <span className="text-sm text-slate-600">Sections</span>
        <span className="text-xs text-slate-500">{questions} questions · {total} points</span>
      </div>
      <ol className="space-y-3">
        {value.sections.map((s, i) => {
          const same = s.points.every((p) => p === s.points[0]);
          const open = s.type === "short" || s.type === "calc" || s.type === "other";
          return (
            <li key={i} className="space-y-2 border border-[var(--color-border)] bg-white p-3">
              <div className="flex flex-wrap items-center gap-2">
                <input className="input-field min-w-48 flex-1 text-sm font-medium" aria-label="Section heading" value={s.title}
                  onChange={(e) => setSection(i, { title: e.target.value })} />
                <select className="input-field w-40 text-sm" aria-label="Question type" value={s.type}
                  onChange={(e) => setSection(i, { type: e.target.value as QuestionType })}>
                  {TYPES.map((t) => <option key={t} value={t}>{TYPE_LABELS[t].replace(" (describe)", "")}</option>)}
                </select>
                <label className="flex items-center gap-1 text-xs text-slate-500">
                  <input className="input-field w-14 px-1 py-1 text-center text-sm" type="number" min={1} max={50} value={s.count}
                    onChange={(e) => setSection(i, resize(s, Math.max(1, Math.min(50, Number(e.target.value) || 1))))} /> questions
                </label>
                {same && !open && (
                  <label className="flex items-center gap-1 text-xs text-slate-500">
                    <input className="input-field w-16 px-1 py-1 text-center text-sm" type="number" min={0} step={0.5} value={s.points[0]}
                      onChange={(e) => setSection(i, { points: s.points.map(() => Number(e.target.value)) })} /> pts each
                  </label>
                )}
                <span className="flex-1" />
                <button type="button" className="p-1 text-slate-400 hover:text-slate-700 disabled:opacity-30" disabled={i === 0}
                  aria-label="Move up" onClick={() => move(i, -1)}><ArrowUp className="h-4 w-4" /></button>
                <button type="button" className="p-1 text-slate-400 hover:text-slate-700 disabled:opacity-30" disabled={i === value.sections.length - 1}
                  aria-label="Move down" onClick={() => move(i, 1)}><ArrowDown className="h-4 w-4" /></button>
                <button type="button" className="p-1 text-slate-400 hover:text-red-600" aria-label="Remove section"
                  onClick={() => set({ sections: renumber(value.sections.filter((_, j) => j !== i)) })}><Trash2 className="h-4 w-4" /></button>
              </div>
              {(open || !same) && (
                <div className="flex flex-wrap gap-2">
                  {s.points.map((p, k) => (
                    <span key={k} className="flex items-center gap-1 border border-[var(--color-border)] px-1.5 py-1 text-xs text-slate-500">
                      {k + 1}.
                      <input className="w-12 border-b border-slate-300 text-center text-sm outline-none" type="number" min={0} step={0.5} value={p}
                        aria-label={`Points of question ${k + 1}`}
                        onChange={(e) => setSection(i, { points: s.points.map((x, j) => (j === k ? Number(e.target.value) : x)) })} />
                      pts
                      {open && (
                        <input className="w-16 border-b border-slate-300 text-sm outline-none" placeholder="split" value={s.parts[k]}
                          aria-label={`Sub-questions of question ${k + 1}`} title="Sub-question points, e.g. 4+6"
                          onChange={(e) => setSection(i, { parts: s.parts.map((x, j) => (j === k ? e.target.value : x)) })} />
                      )}
                    </span>
                  ))}
                </div>
              )}
              <div className="flex flex-wrap items-center gap-3">
                <input className="input-field flex-1 text-xs" placeholder="What the questions look like (optional)" value={s.format}
                  aria-label="Format" onChange={(e) => setSection(i, { format: e.target.value })} />
                <label className="flex items-center gap-1.5 text-xs text-slate-500">
                  <input type="checkbox" checked={s.passage} onChange={(e) => setSection(i, { passage: e.target.checked })} />
                  Shared reading passage
                </label>
              </div>
            </li>
          );
        })}
      </ol>
      <button type="button" className="inline-flex items-center gap-1 text-sm text-[var(--color-primary)] hover:underline"
        onClick={() => set({ sections: renumber([...value.sections, { title: "", type: "single", count: 5, points: [2, 2, 2, 2, 2], parts: ["", "", "", "", ""], format: "", passage: false }]) })}>
        <Plus className="h-4 w-4" /> Add a section
      </button>
    </div>
  );
}

