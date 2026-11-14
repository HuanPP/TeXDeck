import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { Dumbbell, FileUp, Pencil, Plus, RefreshCw, Trash2, Trophy } from "lucide-react";
import {
  addPastPapers, deletePaper, getExam, getGeneration, newDrill, newPaper, redraftProfile, saveProfile,
  type ExamState, type Mastery, type PaperSummary, type Profile, type QuestionType,
} from "../api";
import { TYPE_LABELS } from "./ExamList";
import ProfileEditor from "../components/ProfileEditor";
import StatusBadge from "../components/StatusBadge";
import { useTaskWs } from "../hooks/useTaskWs";
import { prepareFigure } from "../imageTools";
import { displayTitle } from "../title";
import type { GenerationMeta, TaskEvent } from "../types";

// One exam the reader prepares for: its profile (what the exam is like) and
// the mock papers written for it.

const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));
const fmtDay = (ms: number) => new Date(ms).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
const pct = (a: { score: number; total: number }) => (a.total ? Math.round((a.score / a.total) * 100) : 0);

function PaperCard({ id, paper, n, onDelete }: { id: string; paper: PaperSummary; n: number; onDelete: () => void }) {
  const name = paper.drill ? paper.drill.topic : `Paper ${n}`;
  const [confirm, setConfirm] = useState(false);
  const best = paper.attempts.reduce<number | null>((b, a) => Math.max(b ?? 0, pct(a)), null);
  return (
    <li className="relative flex flex-col gap-2 border border-[var(--color-border)] bg-white p-4 transition hover:border-[var(--color-primary)]">
      <Link to={`/exam/${id}/paper/${paper.id}`} className="absolute inset-0" aria-label={`Open ${paper.drill ? "drill" : "paper"} ${n}`} />
      <div className="flex items-baseline justify-between gap-2">
        <span className="min-w-0 truncate text-lg text-[var(--color-heading)]" title={name}>{name}</span>
        <span className="shrink-0 text-xs text-slate-400">{fmtDay(paper.created * 1000)}</span>
      </div>
      <p className="text-sm text-slate-500">
        {paper.drill ? `${TYPE_LABELS[paper.drill.type].replace(" (describe)", "")} · ${paper.questions} questions` : `${paper.questions} questions · ${paper.total} points`}
      </p>
      {paper.focus && <p className="line-clamp-2 text-xs text-slate-500">Focus: {paper.focus}</p>}
      <div className="mt-auto flex items-center justify-between pt-1">
        <span className="flex items-center gap-1.5 text-sm">
          {best === null ? <span className="text-xs text-slate-400">Not tried yet</span> : (
            <><Trophy className="h-4 w-4 text-amber-500" /><span className="font-semibold text-[var(--color-heading)]">{best}%</span>
              <span className="text-xs text-slate-400">best of {paper.attempts.length}</span></>
          )}
        </span>
        <button type="button" className="relative z-10 p-1 text-xs text-slate-300 hover:text-red-600"
          aria-label={`Delete paper ${n}`} onClick={() => (confirm ? onDelete() : setConfirm(true))} onBlur={() => setConfirm(false)}>
          {confirm ? "Delete?" : <Trash2 className="h-4 w-4" />}
        </button>
      </div>
    </li>
  );
}

// Mastery as a word and a colour; null is "not practised yet".
function level(m: number | null): { label: string; bar: string } {
  if (m === null) return { label: "Not practised", bar: "bg-slate-200" };
  if (m >= 0.85) return { label: "Strong", bar: "bg-emerald-500" };
  if (m >= 0.6) return { label: "Getting there", bar: "bg-sky-500" };
  return { label: "Weak", bar: "bg-amber-500" };
}

// Where practice pays most: topics worth many marks and not yet mastered
// (an unpractised topic counts as half known).
const priority = (t: Mastery) => t.weight * (1 - (t.mastery ?? 0.5));

function Topics({ rows, types, disabled, onDrill }: {
  rows: Mastery[]; types: QuestionType[]; disabled: boolean; onDrill: (topic: string, type: QuestionType, count: number) => void;
}) {
  const [open, setOpen] = useState<string | null>(null);
  const [type, setType] = useState<QuestionType>(types[0] ?? "single");
  const [count, setCount] = useState(12);
  const sorted = [...rows].sort((a, b) => priority(b) - priority(a));
  const practised = rows.some((t) => t.answered > 0);
  if (!rows.length) {
    return (
      <p className="border border-dashed border-[var(--color-border)] px-4 py-6 text-center text-sm text-slate-500">
        This exam's profile lists no topics yet. Use Read again or Edit below to add them; then answers count toward each topic.
      </p>
    );
  }
  return (
    <ul className="divide-y divide-[var(--color-border)] border border-[var(--color-border)] bg-white">
      <li className="flex items-center gap-3 px-4 py-1.5 text-xs text-slate-400">
        <span className="flex-1">Topic</span>
        <span className="w-12 text-right">Of marks</span>
        <span className="w-40">You know</span>
        <span className="w-[3.25rem]" />
      </li>
      {sorted.map((t, i) => {
        const l = level(t.mastery);
        return (
          <li key={t.name} className="px-4 py-2.5">
            <div className="flex flex-wrap items-center gap-3">
              <span className="min-w-0 flex-1 truncate text-sm text-[var(--color-heading)]" title={t.name}>
                {t.name}
                {i === 0 && practised && t.weight > 0 && <span className="ml-2 rounded-sm bg-amber-100 px-1.5 py-px text-[10px] text-amber-800">Practise next</span>}
              </span>
              <span className="w-12 text-right text-xs text-slate-400" title="Share of the marks">{t.weight}%</span>
              <span className="flex w-40 items-center gap-2" title={`${t.answered} answered`}>
                <span className="h-2 flex-1 overflow-hidden rounded-full bg-slate-100">
                  <span className={`block h-full rounded-full ${l.bar}`} style={{ width: `${Math.round((t.mastery ?? 0) * 100)}%` }} />
                </span>
                <span className="w-20 text-xs text-slate-500">{t.mastery === null ? l.label : `${Math.round(t.mastery * 100)}%`}</span>
              </span>
              <button type="button" className="inline-flex items-center gap-1 text-xs text-[var(--color-primary)] hover:underline disabled:opacity-40"
                disabled={disabled} onClick={() => setOpen((o) => (o === t.name ? null : t.name))}>
                <Dumbbell className="h-3.5 w-3.5" /> Drill
              </button>
            </div>
            {open === t.name && (
              <div className="mt-2 flex flex-wrap items-center gap-2 text-sm">
                <select className="input-field w-44 px-2 py-1 text-sm" aria-label="Question type" value={type}
                  onChange={(e) => setType(e.target.value as QuestionType)}>
                  {(Object.keys(TYPE_LABELS) as QuestionType[]).map((k) => (
                    <option key={k} value={k}>{TYPE_LABELS[k].replace(" (describe)", "")}{types.includes(k) ? "" : " (not in the exam)"}</option>
                  ))}
                </select>
                <select className="input-field w-36 px-2 py-1 text-sm" aria-label="How many" value={count} onChange={(e) => setCount(Number(e.target.value))}>
                  {[10, 12, 15].map((n) => <option key={n} value={n}>{n} questions</option>)}
                </select>
                <button type="button" className="btn-primary py-1 text-sm" disabled={disabled}
                  onClick={() => { onDrill(t.name, type, count); setOpen(null); }}>
                  Make the drill
                </button>
                <span className="text-xs text-slate-400">From the plainest form to past-paper hard, one change at a time.</span>
              </div>
            )}
          </li>
        );
      })}
    </ul>
  );
}

export default function ExamPage() {
  const { id = "" } = useParams();
  const [meta, setMeta] = useState<GenerationMeta | null>(null);
  const [state, setState] = useState<ExamState | null>(null);
  const [draft, setDraft] = useState<Profile | null>(null);
  const [editing, setEditing] = useState(false);
  const [focus, setFocus] = useState("");
  const [progress, setProgress] = useState<TaskEvent | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [m, s] = await Promise.all([getGeneration(id), getExam(id)]);
      setMeta(m);
      setState(s);
      setDraft(s.profile);
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
      setEditing(false);
      void load();
    }
  }, () => void load());

  async function act(fn: () => Promise<unknown>) {
    setError(null);
    setBusy(true);
    try {
      await fn();
      await load();
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  const writePaper = () => act(async () => {
    if (draft && (editing || state?.stage === "profile")) await saveProfile(id, draft);
    await newPaper(id, focus.trim());
    setFocus("");
    setEditing(false);
  });

  async function addPast(list: FileList | null) {
    if (!list?.length) return;
    const picked = Array.from(list);
    await act(async () => {
      const files: File[] = [];
      for (const f of picked) files.push(f.type === "application/pdf" || f.name.toLowerCase().endsWith(".pdf") ? f : await prepareFigure(f));
      await addPastPapers(id, files);
    });
  }

  if (!meta || !state) {
    return error ? <div role="alert" className="border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">{error}</div>
      : <p className="text-sm text-slate-500">Loading…</p>;
  }

  const profile = state.profile;
  const showEditor = !running && draft && (state.stage === "profile" || editing);
  const mocks = state.papers.filter((p) => !p.drill);
  const drills = state.papers.filter((p) => p.drill).reverse();
  const points = (p: Profile) => p.sections.reduce((n, s) => n + s.points.reduce((a, b) => a + b, 0), 0);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-3">
        <Link to="/learn/exams" className="shrink-0 text-sm text-[var(--color-primary)] no-underline hover:underline">← Exams</Link>
        <h1 className="min-w-0 truncate text-2xl" title={profile?.title || displayTitle(meta)}>{profile?.title || displayTitle(meta)}</h1>
        <StatusBadge status={meta.status} />
      </div>

      {error && <div role="alert" className="border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">{error}</div>}

      {running && (
        <section className="space-y-3 border border-[var(--color-border)] bg-white p-6">
          <p className="text-sm text-slate-600">{progress?.stage || "Working"}…</p>
          <div className="h-2 overflow-hidden rounded-full bg-slate-100">
            <div className="h-full rounded-full bg-[var(--color-primary)]" style={{ width: `${Math.max(4, progress?.percent ?? 4)}%`, transition: "width 0.5s ease" }} />
          </div>
          <p className="text-xs text-slate-400">A minute or a few. You can leave the page meanwhile.</p>
        </section>
      )}

      {meta.status === "failed" && (
        <section role="alert" className="space-y-3 border border-red-200 bg-red-50 p-4 text-sm text-red-800">
          <p>{meta.error || "Something went wrong."}</p>
          <button type="button" className="btn-secondary text-sm" disabled={busy}
            onClick={() => void (profile ? writePaper() : act(() => redraftProfile(id)))}>Try again</button>
        </section>
      )}

      {showEditor && draft && (
        <section className="space-y-5 border border-[var(--color-border)] bg-slate-50/60 p-6">
          <div>
            <h2 className="text-xl">What this exam is like</h2>
            <p className="mt-1 text-sm text-slate-500">
              {state.past.length ? `Read from ${state.past.length} past ${state.past.length === 1 ? "paper" : "papers"}, ` : "Worked out from "}
              your notes and the material. Correct anything: every paper is written to this.
            </p>
          </div>
          <ProfileEditor value={draft} onChange={setDraft} />
          <div className="flex flex-wrap justify-end gap-2">
            {editing && <button type="button" className="btn-secondary" onClick={() => { setEditing(false); setDraft(profile); }}>Cancel</button>}
            {editing && (
              <button type="button" className="btn-secondary" disabled={busy}
                onClick={() => void act(async () => { await saveProfile(id, draft); setEditing(false); })}>Save</button>
            )}
            <button type="button" className="btn-primary" disabled={busy || !draft.sections.length} onClick={() => void writePaper()}>
              {state.stage === "profile" ? "Write the first paper" : "Save and write a paper"}
            </button>
          </div>
        </section>
      )}

      {state.stage === "ready" && profile && !editing && (
        <>
          <section className="space-y-3">
            <div className="flex flex-wrap items-end justify-between gap-3">
              <h2 className="text-xl">Papers</h2>
              <div className="flex min-w-0 flex-1 flex-wrap items-center justify-end gap-2">
                <input className="input-field max-w-md flex-1 px-3 py-2 text-sm" value={focus} onChange={(e) => setFocus(e.target.value)}
                  placeholder="Optional: what this paper should stress" aria-label="What the new paper should stress" disabled={running} />
                <button type="button" className="btn-primary" disabled={busy || running} onClick={() => void writePaper()}>
                  <Plus className="h-4 w-4" /> New paper
                </button>
              </div>
            </div>
            <ol className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {mocks.map((p, i) => (
                <PaperCard key={p.id} id={id} paper={p} n={i + 1} onDelete={() => void act(() => deletePaper(id, p.id))} />
              ))}
            </ol>
            <p className="text-xs text-slate-400">
              Each new paper avoids the questions of the earlier ones{state.mastery.some((m) => m.answered) ? ", and leans toward your weak topics" : ""}.
            </p>
          </section>

          <section className="space-y-3">
            <div>
              <h2 className="text-xl">Topics</h2>
              <p className="mt-1 text-sm text-slate-500">
                How well you know each topic, from every question you have answered here (recent answers count most).
                A drill is 10 to 15 questions of one kind on one topic.
              </p>
            </div>
            <Topics rows={state.mastery} types={[...new Set(profile.sections.map((s) => s.type))]} disabled={busy || running}
              onDrill={(topic, type, count) => void act(() => newDrill(id, { topic, type, count }))} />
            {drills.length > 0 && <h3 className="pt-2 text-lg">Drills</h3>}
            {drills.length > 0 && (
              <ol className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                {drills.map((p, i) => (
                  <PaperCard key={p.id} id={id} paper={p} n={i + 1} onDelete={() => void act(() => deletePaper(id, p.id))} />
                ))}
              </ol>
            )}
          </section>

          <section className="space-y-3 border border-[var(--color-border)] bg-white p-5">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h2 className="text-lg">What this exam is like</h2>
              <div className="flex flex-wrap gap-4 text-sm">
                <label className={`inline-flex items-center gap-1 text-[var(--color-primary)] ${running ? "opacity-40" : "cursor-pointer hover:underline"}`}>
                  <FileUp className="h-4 w-4" /> Add past papers
                  <input type="file" multiple accept="image/*,.heic,.heif,application/pdf,.pdf" className="sr-only" disabled={running}
                    onChange={(e) => { void addPast(e.target.files); e.target.value = ""; }} />
                </label>
                <button type="button" className="inline-flex items-center gap-1 text-[var(--color-primary)] hover:underline disabled:opacity-40"
                  disabled={running} title="Draft this again from the material, the past papers and your notes"
                  onClick={() => void act(() => redraftProfile(id))}>
                  <RefreshCw className="h-4 w-4" /> Read again
                </button>
                <button type="button" className="inline-flex items-center gap-1 text-[var(--color-primary)] hover:underline disabled:opacity-40"
                  disabled={running} onClick={() => { setDraft(profile); setEditing(true); }}>
                  <Pencil className="h-4 w-4" /> Edit
                </button>
              </div>
            </div>
            <p className="text-sm text-slate-600">
              {profile.sections.map((s) => `${s.title} ×${s.count}`).join(" · ")}
              <span className="text-slate-400"> · {points(profile)} points{profile.duration ? ` · ${profile.duration} min` : ""}</span>
            </p>
            {profile.topics.length > 0 && (
              <ul className="flex flex-wrap gap-1.5">
                {[...profile.topics].sort((a, b) => b.weight - a.weight).map((t) => (
                  <li key={t.name} title={t.note} className="border border-[var(--color-border)] bg-slate-50 px-2 py-0.5 text-xs text-slate-600">
                    {t.name} <span className="text-slate-400">{t.weight}%</span>
                  </li>
                ))}
              </ul>
            )}
            <p className="text-xs text-slate-400">
              {state.past.length
                ? `${state.past.length} past ${state.past.length === 1 ? "paper" : "papers"} on file; they are the standard every paper is held to.`
                : "No past papers yet. Adding some brings the papers closer to the real thing."}
            </p>
          </section>
        </>
      )}
    </div>
  );
}
