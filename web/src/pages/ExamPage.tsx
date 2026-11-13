import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowDown, ArrowUp, ChevronDown, Download, Play, Plus, RotateCw, Trash2, Trophy } from "lucide-react";
import {
  examPdfUrl, getExam, getGeneration, redraftBlueprint, saveAttempt, saveBlueprint, writePaper,
  type Attempt, type Blueprint, type BlueprintSection, type ExamState, type QuestionType,
} from "../api";
import ExamPractice from "../components/ExamPractice";
import PdfScroll from "../components/PdfScroll";
import Segmented from "../components/Segmented";
import StatusBadge from "../components/StatusBadge";
import { useTaskWs } from "../hooks/useTaskWs";
import { displayTitle } from "../title";
import type { GenerationMeta, TaskEvent } from "../types";
import { TYPE_LABELS } from "./ExamList";
import { renumber } from "../examNumbering";

// One practice exam: its blueprint while that waits for the reader, then the
// paper, the answer key and the practice.

const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));
const TYPES = Object.keys(TYPE_LABELS) as QuestionType[];
const sum = (xs: number[]) => xs.reduce((a, b) => a + (Number(b) || 0), 0);

function BlueprintEditor({ value, onChange }: { value: Blueprint; onChange: (b: Blueprint) => void }) {
  const set = (patch: Partial<Blueprint>) => onChange({ ...value, ...patch });
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
      {value.style && (
        <label className="block text-sm text-slate-600">Style taken from the samples
          <textarea className="input-field mt-1 min-h-16 text-sm" value={value.style} onChange={(e) => set({ style: e.target.value })} />
        </label>
      )}
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

export default function ExamPage() {
  const { id = "" } = useParams();
  const [meta, setMeta] = useState<GenerationMeta | null>(null);
  const [state, setState] = useState<ExamState | null>(null);
  const [draft, setDraft] = useState<Blueprint | null>(null);
  const [progress, setProgress] = useState<TaskEvent | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [view, setView] = useState<"practice" | "paper" | "answers">("practice");
  const [practising, setPractising] = useState<string[] | null | false>(false); // false: not; null: all; list: these
  const [lastWrong, setLastWrong] = useState<string[]>([]);
  const [editing, setEditing] = useState(false);
  const [menu, setMenu] = useState(false);

  const load = useCallback(async () => {
    try {
      const [m, s] = await Promise.all([getGeneration(id), getExam(id)]);
      setMeta(m);
      setState(s);
      setDraft((d) => d ?? s.blueprint);
    } catch (e) {
      setError(errMsg(e));
    }
  }, [id]);
  useEffect(() => { void load(); }, [load]);

  const running = meta?.status === "running";
  useTaskWs(id, running, (event) => {
    setProgress(event);
    if (event.status !== "running") {
      setProgress(null);
      setDraft(null);
      void load();
    }
  }, () => void load());

  async function start(job: "paper" | "redraft") {
    setError(null);
    setBusy(true);
    try {
      if (job === "paper" && draft) await saveBlueprint(id, draft);
      await (job === "paper" ? writePaper(id) : redraftBlueprint(id));
      setEditing(false);
      setPractising(false);
      await load();
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  if (!meta || !state) {
    return error ? <div role="alert" className="border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">{error}</div>
      : <p className="text-sm text-slate-500">Loading…</p>;
  }

  const exam = state.exam;
  const best = state.attempts.reduce<Attempt | null>((b, a) => (!b || a.score / a.total > b.score / b.total ? a : b), null);
  const failed = meta.status === "failed";
  const showBlueprint = !running && draft && (state.stage === "blueprint" || editing || (failed && !exam));

  if (exam && practising !== false) {
    return (
      <ExamPractice id={id} exam={exam} only={practising ?? undefined} onExit={() => setPractising(false)}
        onFinish={(s) => {
          setLastWrong(s.wrong);
          if (s.full) void saveAttempt(id, { score: s.score, total: s.total, answered: s.answered, correct: s.correct })
            .then((attempts) => setState((st) => st && { ...st, attempts }), () => undefined);
        }} />
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-3">
          <Link to="/learn/exams" className="shrink-0 text-sm text-[var(--color-primary)] no-underline hover:underline">← Exams</Link>
          <h1 className="min-w-0 truncate text-2xl">{exam?.title || displayTitle(meta)}</h1>
          <StatusBadge status={meta.status} />
        </div>
        {exam && !running && (
          <div className="relative">
            <button type="button" className="inline-flex items-center gap-1 px-2 py-1 text-sm text-slate-500 hover:text-slate-800"
              aria-haspopup="menu" aria-expanded={menu} onClick={() => setMenu((o) => !o)}>
              <Download className="h-4 w-4" /> Export <ChevronDown className="h-3.5 w-3.5" />
            </button>
            {menu && <div className="fixed inset-0 z-30" onClick={() => setMenu(false)} />}
            {menu && (
              <div role="menu" className="absolute right-0 top-full z-40 w-52 border border-[var(--color-border)] bg-white py-1 text-sm shadow-lg" onClick={() => setMenu(false)}>
                {state.paper && <a role="menuitem" className="flex items-center gap-2 px-3 py-2 text-slate-700 no-underline hover:bg-slate-50" href={examPdfUrl(id, "paper", true)}><Download className="h-4 w-4" /> Exam paper PDF</a>}
                {state.answers && <a role="menuitem" className="flex items-center gap-2 px-3 py-2 text-slate-700 no-underline hover:bg-slate-50" href={examPdfUrl(id, "answers", true)}><Download className="h-4 w-4" /> Answer key PDF</a>}
                <div className="my-1 border-t border-[var(--color-border)]" />
                <button type="button" role="menuitem" className="flex w-full items-center gap-2 px-3 py-2 text-slate-700 hover:bg-slate-50" onClick={() => setEditing(true)}>
                  Edit the structure
                </button>
                <button type="button" role="menuitem" className="flex w-full items-center gap-2 px-3 py-2 text-slate-700 hover:bg-slate-50" onClick={() => void start("paper")}>
                  <RotateCw className="h-4 w-4" /> New questions
                </button>
              </div>
            )}
          </div>
        )}
      </div>

      {error && <div role="alert" className="border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">{error}</div>}

      {running && (
        <section className="space-y-3 border border-[var(--color-border)] bg-white p-6">
          <p className="text-sm text-slate-600">{progress?.stage || (state.stage === "drafting" ? "Reading the sample papers" : "Writing the questions")}…</p>
          <div className="h-2 overflow-hidden rounded-full bg-slate-100">
            <div className="h-full rounded-full bg-[var(--color-primary)]" style={{ width: `${Math.max(4, progress?.percent ?? 4)}%`, transition: "width 0.5s ease" }} />
          </div>
          <p className="text-xs text-slate-400">This takes a minute or two. You can leave the page; the exam appears in the list when it is ready.</p>
        </section>
      )}

      {failed && (
        <section role="alert" className="space-y-3 border border-red-200 bg-red-50 p-4 text-sm text-red-800">
          <p>{meta.error || "Something went wrong."}</p>
          <button type="button" className="btn-secondary text-sm" disabled={busy}
            onClick={() => void start(state.blueprint ? "paper" : "redraft")}>Try again</button>
        </section>
      )}

      {showBlueprint && draft && (
        <section className="space-y-5 border border-[var(--color-border)] bg-slate-50/60 p-6">
          <div>
            <h2 className="text-xl">Check the structure</h2>
            <p className="mt-1 text-sm text-slate-500">
              {state.samples.length ? "Read from your sample papers and your table. " : ""}
              Change anything, then the questions are written to it.
            </p>
          </div>
          <BlueprintEditor value={draft} onChange={setDraft} />
          <div className="flex flex-wrap justify-end gap-2">
            {editing && <button type="button" className="btn-secondary" onClick={() => { setEditing(false); setDraft(state.blueprint); }}>Cancel</button>}
            {state.samples.length > 0 && (
              <button type="button" className="btn-secondary" disabled={busy} onClick={() => void start("redraft")}>Read the samples again</button>
            )}
            <button type="button" className="btn-primary" disabled={busy || !draft.sections.length} onClick={() => void start("paper")}>
              {busy ? "Starting…" : exam ? "Write new questions" : "Write the exam"}
            </button>
          </div>
        </section>
      )}

      {exam && !running && !editing && (
        <>
          <Segmented label="View" value={view} onChange={setView} options={[
            { value: "practice", label: "Practice" }, { value: "paper", label: "Paper" }, { value: "answers", label: "Answer key" },
          ]} />
          {view === "practice" ? (
            <section className="grid gap-6 md:grid-cols-[1fr_260px]">
              <div className="space-y-4 border border-[var(--color-border)] bg-white p-6">
                <p className="text-sm text-slate-500">
                  {exam.sections.reduce((n, s) => n + s.questions.length, 0)} questions · {exam.total} points
                  {exam.duration ? ` · ${exam.duration} minutes on paper` : ""}
                </p>
                <ul className="space-y-1.5 text-sm">
                  {exam.sections.map((s) => (
                    <li key={s.title} className="flex justify-between gap-3 border-b border-dashed border-[var(--color-border)] pb-1.5">
                      <span>{s.title}</span>
                      <span className="text-slate-400">{s.questions.length} · {sum(s.questions.map((q) => q.points))} pts</span>
                    </li>
                  ))}
                </ul>
                <div className="flex flex-wrap gap-3 pt-2">
                  <button type="button" className="inline-flex items-center gap-2 rounded-xl bg-emerald-500 px-8 py-3 font-semibold text-white shadow-[0_4px_0_0_#059669] hover:bg-emerald-600"
                    onClick={() => setPractising(null)}>
                    <Play className="h-4 w-4" /> Start
                  </button>
                  {lastWrong.length > 0 && (
                    <button type="button" className="btn-secondary" onClick={() => setPractising(lastWrong)}>
                      Retry the {lastWrong.length} I missed
                    </button>
                  )}
                </div>
                <p className="text-xs text-slate-400">Choice, true/false and fill-in questions are marked at once; written answers are marked by the AI against the model answer, or by you.</p>
              </div>
              <aside className="space-y-3 border border-[var(--color-border)] bg-white p-5 text-sm">
                <p className="flex items-center gap-2 text-slate-500"><Trophy className="h-4 w-4 text-amber-500" /> Attempts</p>
                {state.attempts.length === 0 ? <p className="text-xs text-slate-400">None yet.</p> : (
                  <>
                    {best && <p className="text-2xl font-semibold text-[var(--color-heading)]">{Math.round((best.score / best.total) * 100)}%<span className="ml-2 text-xs font-normal text-slate-400">best</span></p>}
                    <ol className="space-y-1 text-xs text-slate-500">
                      {[...state.attempts].reverse().slice(0, 8).map((a) => (
                        <li key={a.at} className="flex justify-between"><span>{new Date(a.at * 1000).toLocaleDateString()}</span><span className="font-mono">{a.score} / {a.total}</span></li>
                      ))}
                    </ol>
                  </>
                )}
              </aside>
            </section>
          ) : (
            <section className="h-[75vh] border border-[var(--color-border)] bg-white">
              <PdfScroll key={view} file={examPdfUrl(id, view)} label={view === "paper" ? "Exam paper" : "Answer key"} />
            </section>
          )}
        </>
      )}
    </div>
  );
}
