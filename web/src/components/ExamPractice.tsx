import { useEffect, useMemo, useRef, useState } from "react";
import { Check, CircleDot, Flame, ImagePlus, Loader2, Sparkles, X } from "lucide-react";
import { getGrade, gradeAnswer, type Exam, type ExamQuestion, type Grade } from "../api";
import { prepareFigure } from "../imageTools";
import { playDone, playFinish, playWrong } from "../sounds";
import ChatMarkdown from "./ChatMarkdown";

// Practice an exam one question at a time: answer, check, see why, go on.
// Choice, true/false and fill-in questions are marked here; open questions
// are marked by the model (or by the reader against the model answer).

const LETTERS = "ABCDEFGH";

const TONES = {
  good: { bar: "border-emerald-300 bg-emerald-50", dot: "bg-emerald-500", text: "text-emerald-700", button: "bg-emerald-500 hover:bg-emerald-600" },
  part: { bar: "border-amber-300 bg-amber-50", dot: "bg-amber-500", text: "text-amber-700", button: "bg-amber-500 hover:bg-amber-600" },
  bad: { bar: "border-red-200 bg-red-50", dot: "bg-red-500", text: "text-red-700", button: "bg-red-500 hover:bg-red-600" },
};

type Result = { score: number; max: number; correct: boolean; feedback?: string; reference: string };
type Item = { q: ExamQuestion; section: string; passage: string };

const md = (text: string) => text.replace(/\{\{(\d+)\}\}/g, (_, n: string) => ` **(${n})**\\_\\_\\_\\_ `).replace(/(?<!\n)\n(?!\n)/g, "  \n");

// Fill-in answers: same text after Unicode folding, case, spaces and trailing
// punctuation; or the same number.
export function sameAnswer(given: string, accepted: string): boolean {
  const norm = (s: string) => s.normalize("NFKC").toLowerCase().replace(/\s+/g, "").replace(/[.,;:!?。，；：！？]+$/u, "");
  const a = norm(given);
  const b = norm(accepted);
  if (!a) return false;
  if (a === b) return true;
  const x = Number(a);
  const y = Number(b);
  return Number.isFinite(x) && Number.isFinite(y) && Math.abs(x - y) <= 1e-6 * Math.max(1, Math.abs(y));
}

function referenceOf(q: ExamQuestion): string {
  if (q.mode === "single") return `**${q.answer as string}.** ${q.options?.[LETTERS.indexOf(q.answer as string)] ?? ""}`;
  if (q.mode === "multiple") return (q.answer as string[]).map((l) => `**${l}.** ${q.options?.[LETTERS.indexOf(l)] ?? ""}`).join("  \n");
  if (q.mode === "truefalse") return q.answer ? "**True**" : "**False**";
  if (q.mode === "fill") return (q.answer as string[][]).map((alts, i) => `(${i + 1}) ${alts.join(" / ")}`).join("  \n");
  const parts = q.parts?.map((p) => `${p.stem}\n\n${p.answer}`).join("\n\n") ?? "";
  return [q.answer, parts].filter(Boolean).join("\n\n");
}

function markObjective(q: ExamQuestion, choice: string[], blanks: string[], truth: boolean | null): Result {
  const reference = referenceOf(q);
  let fraction = 0;
  if (q.mode === "single") fraction = choice[0] === q.answer ? 1 : 0;
  else if (q.mode === "truefalse") fraction = truth === q.answer ? 1 : 0;
  else if (q.mode === "multiple") {
    const want = new Set(q.answer as string[]);
    const wrong = choice.some((c) => !want.has(c));
    fraction = wrong ? 0 : choice.length === want.size ? 1 : choice.length ? 0.5 : 0;
  } else if (q.mode === "fill") {
    const lists = q.answer as string[][];
    const right = lists.filter((alts, i) => alts.some((a) => sameAnswer(blanks[i] ?? "", a))).length;
    fraction = right / lists.length;
  }
  const score = Math.round(q.points * fraction * 2) / 2;
  return { score, max: q.points, correct: fraction === 1, reference };
}

// A practice run in progress, kept in the browser so leaving or reloading the
// page does not lose it.
export type SavedRun = { only: string[] | null; results: Record<string, Result>; streak: number; best: number };
const runKey = (id: string, pid: string) => `texdeck.practice.${id}.${pid}`;

export function savedRun(id: string, pid: string): SavedRun | null {
  try {
    const run = JSON.parse(localStorage.getItem(runKey(id, pid)) ?? "null") as SavedRun | null;
    return run && Object.keys(run.results).length ? run : null;
  } catch {
    return null;
  }
}

export const forgetRun = (id: string, pid: string) => localStorage.removeItem(runKey(id, pid));

type Props = {
  id: string;
  pid: string; // the paper
  exam: Exam;
  only?: string[]; // question ids to practise (retrying mistakes); all when absent
  resume?: SavedRun; // carry on from here
  onExit: () => void;
  onResult?: (qid: string, score: number) => void; // each marked question, for the topic's mastery
  onFinish: (summary: { score: number; total: number; answered: number; correct: number; wrong: string[]; full: boolean }) => void;
};

export default function ExamPractice({ id, pid, exam, only: asked, resume, onExit, onFinish, onResult }: Props) {
  const only = resume ? resume.only ?? undefined : asked;
  const items = useMemo<Item[]>(() => exam.sections.flatMap((s) => s.questions
    .filter((q) => !only || only.includes(q.id))
    .map((q) => ({ q, section: s.title, passage: s.passage }))), [exam, only]);
  // Resuming: the first question not yet answered (questions go in order).
  const [at, setAt] = useState(() => {
    if (!resume) return 0;
    const first = items.findIndex((it) => !resume.results[it.q.id]);
    return first < 0 ? Math.max(0, items.length - 1) : first;
  });
  const [choice, setChoice] = useState<string[]>([]);
  const [truth, setTruth] = useState<boolean | null>(null);
  const [blanks, setBlanks] = useState<string[]>([]);
  const [texts, setTexts] = useState<string[]>([]);
  const [photo, setPhoto] = useState<File | null>(null);
  const [result, setResult] = useState<Result | null>(null);
  const [grading, setGrading] = useState(false);
  const [selfMark, setSelfMark] = useState(false); // open question: showing the model answer to mark oneself
  const [error, setError] = useState<string | null>(null);
  const [results, setResults] = useState<Record<string, Result>>(resume?.results ?? {});
  const [streak, setStreak] = useState(resume?.streak ?? 0);
  const [best, setBest] = useState(resume?.best ?? 0);
  const [done, setDone] = useState(false);
  const [showPassage, setShowPassage] = useState(true);
  const cancelled = useRef(false);
  useEffect(() => () => { cancelled.current = true; }, []);

  useEffect(() => {
    if (done || !Object.keys(results).length) return;
    const run: SavedRun = { only: only ?? null, results, streak, best };
    localStorage.setItem(runKey(id, pid), JSON.stringify(run));
  }, [id, pid, only, results, streak, best, done]);

  const item = items[at];
  const q = item?.q;
  const parts = q?.parts ?? [];

  useEffect(() => {
    setChoice([]); setTruth(null); setBlanks([]); setTexts([]); setPhoto(null);
    setResult(null); setSelfMark(false); setError(null); setGrading(false);
  }, [at]);

  function record(r: Result) {
    if (!q) return;
    setResult(r);
    setResults((all) => ({ ...all, [q.id]: r }));
    onResult?.(q.id, r.score);
    if (r.correct) {
      playDone();
      setStreak((s) => { setBest((b) => Math.max(b, s + 1)); return s + 1; });
    } else {
      playWrong();
      setStreak(0);
    }
  }

  const answered = q && (q.mode === "single" || q.mode === "multiple" ? choice.length > 0
    : q.mode === "truefalse" ? truth !== null
      : q.mode === "fill" ? blanks.some((b) => b.trim())
        : texts.some((t) => t.trim()) || photo !== null);

  async function check() {
    if (!q || !answered || result || grading) return;
    if (q.mode !== "open") return record(markObjective(q, choice, blanks, truth));
    setGrading(true);
    setError(null);
    try {
      // One grade per part when the question has parts; else one for the whole.
      // A part left blank (the photo goes with part 1) scores nothing, as on paper.
      const blank = (i: number): Promise<Grade> => Promise.resolve(
        { id: "", qid: q.id, part: i, status: "done", score: 0, max: parts[i].points, feedback: "No answer." });
      const asks = parts.length
        ? parts.map((_, i) => ((texts[i] ?? "").trim() || (i === 0 && photo)
          ? gradeAnswer(id, pid, q.id, i, texts[i] ?? "", i === 0 ? photo ?? undefined : undefined) : blank(i)))
        : [gradeAnswer(id, pid, q.id, null, texts[0] ?? "", photo ?? undefined)];
      let grades = await Promise.all(asks);
      while (grades.some((g) => g.status === "pending")) {
        await new Promise((r) => window.setTimeout(r, 2000));
        if (cancelled.current) return;
        grades = await Promise.all(grades.map((g) => (g.status === "pending" ? getGrade(id, g.id) : g)));
      }
      const failed = grades.find((g) => g.status === "error");
      if (failed) throw new Error(failed.feedback);
      const score = grades.reduce((s, g) => s + g.score, 0);
      const feedback = grades.map((g, i) => (parts.length ? `**${parts[i].stem.split(/\s/)[0]}** ${g.feedback}` : g.feedback)).join("\n\n");
      record({ score, max: q.points, correct: score >= q.points * 0.6, feedback, reference: referenceOf(q) });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setGrading(false);
    }
  }

  function markSelf(fraction: number) {
    if (!q) return;
    record({ score: Math.round(q.points * fraction * 2) / 2, max: q.points, correct: fraction >= 0.6, reference: referenceOf(q) });
  }

  function next() {
    if (at + 1 < items.length) return setAt(at + 1);
    setDone(true);
    forgetRun(id, pid);
    playFinish();
    const all = Object.entries(results);
    const score = all.reduce((s, [, r]) => s + r.score, 0);
    onFinish({
      score, total: items.reduce((s, it) => s + it.q.points, 0), answered: all.length,
      correct: all.filter(([, r]) => r.correct).length,
      wrong: items.filter((it) => !results[it.q.id]?.correct).map((it) => it.q.id), full: !only,
    });
  }

  // Keys: 1-8 pick options, T/F, Enter checks and goes on.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!q || done) return;
      const typing = e.target instanceof HTMLTextAreaElement || e.target instanceof HTMLInputElement;
      if (e.key === "Enter" && !e.shiftKey && !(e.target instanceof HTMLTextAreaElement)) {
        e.preventDefault();
        if (result) next(); else void check();
        return;
      }
      if (typing || result) return;
      const n = Number(e.key) - 1;
      if (q.options && n >= 0 && n < q.options.length) {
        const l = LETTERS[n];
        setChoice((c) => (q.mode === "multiple" ? (c.includes(l) ? c.filter((x) => x !== l) : [...c, l].sort()) : [l]));
      }
      if (q.mode === "truefalse" && (e.key === "t" || e.key === "f")) setTruth(e.key === "t");
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const score = Object.values(results).reduce((s, r) => s + r.score, 0);
  const total = items.reduce((s, it) => s + it.q.points, 0);

  if (done) {
    const correct = Object.values(results).filter((r) => r.correct).length;
    const pct = total ? Math.round((score / total) * 100) : 0;
    const wrong = items.filter((it) => !results[it.q.id]?.correct);
    return (
      <div className="notebook-chat mx-auto max-w-2xl space-y-6 py-6 text-center">
        <div className="learn-pop mx-auto flex h-36 w-36 flex-col items-center justify-center rounded-full border-8 border-emerald-400 bg-emerald-50">
          <span className="text-4xl font-semibold text-emerald-700">{pct}%</span>
          <span className="text-xs text-emerald-700">{score} / {total}</span>
        </div>
        <h2 className="text-2xl">{pct >= 90 ? "Excellent" : pct >= 70 ? "Well done" : pct >= 50 ? "Getting there" : "Keep practising"}</h2>
        <p className="text-sm text-slate-600">
          {correct} of {items.length} right · best streak {best}
        </p>
        {wrong.length > 0 && (
          <div className="space-y-3 text-left">
            <h3 className="text-sm text-slate-500">To look at again</h3>
            {wrong.map((it) => (
              <div key={it.q.id} className="border border-[var(--color-border)] bg-white p-4 text-sm">
                <p className="mb-1 text-xs text-slate-400">Question {it.q.number} · {results[it.q.id]?.score ?? 0} / {it.q.points}</p>
                <ChatMarkdown text={md(it.q.stem)} />
                <div className="mt-2 border-l-2 border-emerald-400 pl-3 text-slate-700"><ChatMarkdown text={referenceOf(it.q)} /></div>
              </div>
            ))}
          </div>
        )}
        <div className="flex justify-center gap-3">
          <button type="button" className="btn-secondary" onClick={onExit}>Back to the exam</button>
        </div>
      </div>
    );
  }
  if (!q) return null;

  // A result reads right, partly right (some credit) or wrong.
  const tone: keyof typeof TONES = !result ? "good" : result.correct ? "good" : result.score > 0 ? "part" : "bad";

  const optionClass = (l: string) => {
    const picked = choice.includes(l);
    if (result) {
      const right = q.mode === "multiple" ? (q.answer as string[]).includes(l) : q.answer === l;
      if (right) return "border-emerald-500 bg-emerald-50 text-emerald-900";
      if (picked) return "border-red-400 bg-red-50 text-red-800";
      return "border-[var(--color-border)] opacity-60";
    }
    return picked ? "border-indigo-500 bg-indigo-50 shadow-[0_3px_0_0_#6366f1]" : "border-[var(--color-border)] bg-white shadow-[0_3px_0_0_#e2e8f0] hover:bg-slate-50";
  };

  return (
    <div className="notebook-chat mx-auto flex min-h-[70vh] max-w-3xl flex-col">
      <div className="flex items-center gap-4 py-3">
        <button type="button" className="p-1 text-slate-400 hover:text-slate-700" aria-label="Stop practising" onClick={onExit}>
          <X className="h-5 w-5" />
        </button>
        <div className="h-3 flex-1 overflow-hidden rounded-full bg-slate-200" role="progressbar" aria-valuenow={at} aria-valuemax={items.length}>
          <div className="h-full rounded-full bg-emerald-500" style={{ width: `${((at + (result ? 1 : 0)) / items.length) * 100}%`, transition: "width 0.4s ease" }} />
        </div>
        <span className={`inline-flex items-center gap-1 text-sm font-semibold ${streak ? "text-orange-500" : "text-slate-300"}`} title="Right in a row">
          <Flame className={`h-5 w-5 ${streak >= 3 ? "learn-pop" : ""}`} key={streak} /> {streak}
        </span>
        <span className="font-mono text-xs text-slate-500">{score} pts</span>
      </div>

      <div className="flex-1 space-y-4 pb-6">
        <p className="text-xs uppercase tracking-wide text-slate-400">{item.section} · {at + 1} / {items.length} · {q.points} {q.points === 1 ? "point" : "points"}</p>
        {item.passage && (
          <div className="border border-[var(--color-border)] bg-slate-50 text-sm">
            <button type="button" className="w-full px-4 py-2 text-left text-xs text-slate-500 hover:text-slate-800"
              onClick={() => setShowPassage((s) => !s)}>{showPassage ? "Hide the passage" : "Show the passage"}</button>
            {showPassage && <div className="max-h-72 overflow-auto px-4 pb-3"><ChatMarkdown text={md(item.passage)} /></div>}
          </div>
        )}
        <div className="text-lg leading-relaxed text-[var(--color-heading)]"><ChatMarkdown text={md(q.stem)} /></div>

        {q.options && (
          <div className="grid gap-3">
            {q.options.map((o, i) => {
              const l = LETTERS[i];
              return (
                <button key={l} type="button" disabled={Boolean(result)} aria-pressed={choice.includes(l)}
                  className={`flex items-start gap-3 rounded-xl border-2 px-4 py-3 text-left transition ${optionClass(l)}`}
                  onClick={() => setChoice((c) => (q.mode === "multiple" ? (c.includes(l) ? c.filter((x) => x !== l) : [...c, l].sort()) : [l]))}>
                  <span className="mt-0.5 inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-md border border-current text-xs">{l}</span>
                  <span className="min-w-0 flex-1"><ChatMarkdown text={o} /></span>
                </button>
              );
            })}
            {q.mode === "multiple" && !result && <p className="text-xs text-slate-400">Choose every right answer.</p>}
          </div>
        )}

        {q.mode === "truefalse" && (
          <div className="grid grid-cols-2 gap-3">
            {[true, false].map((v) => {
              const picked = truth === v;
              const cls = result
                ? (q.answer === v ? "border-emerald-500 bg-emerald-50" : picked ? "border-red-400 bg-red-50" : "border-[var(--color-border)] opacity-60")
                : picked ? "border-indigo-500 bg-indigo-50 shadow-[0_3px_0_0_#6366f1]" : "border-[var(--color-border)] bg-white shadow-[0_3px_0_0_#e2e8f0] hover:bg-slate-50";
              return (
                <button key={String(v)} type="button" disabled={Boolean(result)} aria-pressed={picked}
                  className={`rounded-xl border-2 px-4 py-4 text-lg ${cls}`} onClick={() => setTruth(v)}>
                  {v ? "True" : "False"}
                </button>
              );
            })}
          </div>
        )}

        {q.mode === "fill" && (
          <div className="space-y-2">
            {(q.answer as string[][]).map((_, i) => (
              <label key={i} className="flex items-center gap-3 text-sm text-slate-600">
                <span className="w-8">({i + 1})</span>
                <input className="input-field max-w-sm text-base" disabled={Boolean(result)} value={blanks[i] ?? ""} autoFocus={i === 0}
                  onChange={(e) => setBlanks((b) => { const n = [...b]; n[i] = e.target.value; return n; })} />
              </label>
            ))}
          </div>
        )}

        {q.mode === "open" && (
          <div className="space-y-3">
            {(parts.length ? parts : [null]).map((p, i) => (
              <div key={i} className="space-y-1">
                {p && <div className="flex items-baseline gap-2 text-sm text-slate-700"><span className="flex-1"><ChatMarkdown text={md(p.stem)} /></span><span className="text-xs text-slate-400">{p.points} pts</span></div>}
                <textarea className="input-field min-h-28 text-sm" disabled={Boolean(result) || grading} value={texts[i] ?? ""}
                  placeholder="Your answer (maths as $...$ if you like)"
                  onChange={(e) => setTexts((t) => { const n = [...t]; n[i] = e.target.value; return n; })} />
              </div>
            ))}
            {!result && (
              <label className="inline-flex cursor-pointer items-center gap-1.5 text-xs text-slate-500 hover:text-slate-800">
                <ImagePlus className="h-4 w-4" /> {photo ? `Photo: ${photo.name}` : "Add a photo of a handwritten answer"}
                <input type="file" accept="image/*,.heic,.heif" className="sr-only" onChange={async (e) => {
                  const f = e.target.files?.[0];
                  e.target.value = "";
                  if (f) try { setPhoto(await prepareFigure(f)); } catch (err) { setError(err instanceof Error ? err.message : String(err)); }
                }} />
              </label>
            )}
            {selfMark && !result && (
              <div className="space-y-3 border border-[var(--color-border)] bg-slate-50 p-4 text-sm">
                <p className="text-xs text-slate-500">Model answer</p>
                <ChatMarkdown text={referenceOf(q)} />
                {q.rubric && q.rubric.length > 0 && <ChatMarkdown text={q.rubric.map((r) => `- ${r}`).join("\n")} />}
                <div className="flex flex-wrap gap-2">
                  <span className="self-center text-xs text-slate-500">How did you do?</span>
                  <button type="button" className="btn-secondary text-sm" onClick={() => markSelf(1)}>Got it</button>
                  <button type="button" className="btn-secondary text-sm" onClick={() => markSelf(0.5)}>Partly</button>
                  <button type="button" className="btn-secondary text-sm" onClick={() => markSelf(0)}>Missed it</button>
                </div>
              </div>
            )}
          </div>
        )}
        {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
      </div>

      <div className={`sticky bottom-0 -mx-4 border-t-2 px-6 py-4 ${result ? TONES[tone].bar : "border-[var(--color-border)] bg-white"}`}>
        {result ? (
          <div className="flex flex-wrap items-start gap-4">
            <div className={`learn-pop flex h-10 w-10 shrink-0 items-center justify-center rounded-full ${TONES[tone].dot} text-white`}>
              {tone === "good" ? <Check className="h-6 w-6" strokeWidth={3} /> : tone === "part" ? <CircleDot className="h-6 w-6" strokeWidth={3} /> : <X className="h-6 w-6" strokeWidth={3} />}
            </div>
            <div className="min-w-0 flex-1 space-y-1 text-sm">
              <p className={`text-lg font-semibold ${TONES[tone].text}`}>
                {result.correct ? (streak >= 3 ? `${streak} in a row!` : "Correct!") : result.score > 0 ? "Partly right" : "Not quite"}
                <span className="ml-2 font-mono text-sm font-normal">{result.score} / {result.max}</span>
              </p>
              {result.feedback && <div className="text-slate-700"><ChatMarkdown text={result.feedback} /></div>}
              {(!result.correct || q.mode === "open") && (
                <details open={!result.correct && q.mode !== "open"} className="text-slate-700">
                  <summary className="cursor-pointer text-xs text-slate-500">{q.mode === "open" ? "Model answer" : "Correct answer"}</summary>
                  <ChatMarkdown text={result.reference} />
                </details>
              )}
              {q.explanation && <div className="text-slate-600"><ChatMarkdown text={q.explanation} /></div>}
            </div>
            <button type="button" autoFocus className={`self-center rounded-xl px-8 py-3 font-semibold text-white shadow-[0_4px_0_0_rgba(0,0,0,0.15)] ${TONES[tone].button}`}
              onClick={next}>
              {at + 1 < items.length ? "Continue" : "See results"}
            </button>
          </div>
        ) : (
          <div className="flex items-center justify-between gap-3">
            <button type="button" className="text-sm text-slate-400 hover:text-slate-700"
              onClick={() => (q.mode === "open" ? setSelfMark(true) : record({ ...markObjective(q, [], [], null), score: 0, correct: false }))}>
              {q.mode === "open" ? "Show the model answer" : "Skip"}
            </button>
            <button type="button" disabled={!answered || grading}
              className="inline-flex items-center gap-2 rounded-xl bg-emerald-500 px-8 py-3 font-semibold text-white shadow-[0_4px_0_0_#059669] hover:bg-emerald-600 disabled:bg-slate-200 disabled:text-slate-400 disabled:shadow-none"
              onClick={() => void check()}>
              {grading ? <><Loader2 className="h-4 w-4 animate-spin" /> Grading…</> : q.mode === "open" ? <><Sparkles className="h-4 w-4" /> Check with AI</> : "Check"}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

