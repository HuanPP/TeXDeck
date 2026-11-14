import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ChevronDown, Download, Play, Trophy } from "lucide-react";
import { examPdfUrl, getPaper, saveAttempt, saveResult, type Exam } from "../api";
import ExamPractice, { forgetRun, savedRun, type SavedRun } from "../components/ExamPractice";
import PdfScroll from "../components/PdfScroll";
import Segmented from "../components/Segmented";

// One mock paper of an exam: practise it, read it, or read its answer key.

const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));
const fmtDay = (ms: number) => new Date(ms).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
const sum = (xs: number[]) => xs.reduce((a, b) => a + (Number(b) || 0), 0);

export default function ExamPaperPage() {
  const { id = "", pid = "" } = useParams();
  const [paper, setPaper] = useState<Exam | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<"practice" | "paper" | "answers">("practice");
  const [practising, setPractising] = useState<string[] | null | false>(false); // false: not; null: all; list: these
  const [lastWrong, setLastWrong] = useState<string[]>([]);
  const [menu, setMenu] = useState(false);
  const [resume, setResume] = useState<SavedRun | null>(null);
  const [saved, setSaved] = useState<SavedRun | null>(() => savedRun(id, pid));

  useEffect(() => {
    getPaper(id, pid).then(setPaper, (e) => setError(errMsg(e)));
  }, [id, pid]);

  if (!paper) {
    return error ? <div role="alert" className="border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">{error}</div>
      : <p className="text-sm text-slate-500">Loading…</p>;
  }

  if (practising !== false) {
    return (
      <ExamPractice id={id} pid={pid} exam={paper} only={practising ?? undefined} resume={resume ?? undefined}
        onExit={() => { setPractising(false); setResume(null); setSaved(savedRun(id, pid)); }}
        onResult={(qid, score) => void saveResult(id, pid, qid, score).catch(() => undefined)}
        onFinish={(s) => {
          setLastWrong(s.wrong);
          setSaved(null);
          if (s.full) void saveAttempt(id, pid, { score: s.score, total: s.total, answered: s.answered, correct: s.correct })
            .then((attempts) => setPaper((p) => p && { ...p, attempts }), () => undefined);
        }} />
    );
  }

  const best = paper.attempts.reduce<number | null>((b, a) => Math.max(b ?? 0, a.total ? Math.round((a.score / a.total) * 100) : 0), null);
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-3">
          <Link to={`/exam/${id}`} className="shrink-0 text-sm text-[var(--color-primary)] no-underline hover:underline">← Exam</Link>
          <h1 className="min-w-0 truncate text-2xl">{paper.title}</h1>
          <span className="text-sm text-slate-400">{paper.drill ? `Drill · ${paper.drill.count} questions, getting harder` : `Paper ${Number(pid)}`}</span>
        </div>
        <div className="relative">
          <button type="button" className="inline-flex items-center gap-1 px-2 py-1 text-sm text-slate-500 hover:text-slate-800"
            aria-haspopup="menu" aria-expanded={menu} onClick={() => setMenu((o) => !o)}>
            <Download className="h-4 w-4" /> Export <ChevronDown className="h-3.5 w-3.5" />
          </button>
          {menu && <div className="fixed inset-0 z-30" onClick={() => setMenu(false)} />}
          {menu && (
            <div role="menu" className="absolute right-0 top-full z-40 w-52 border border-[var(--color-border)] bg-white py-1 text-sm shadow-lg" onClick={() => setMenu(false)}>
              <a role="menuitem" className="flex items-center gap-2 px-3 py-2 text-slate-700 no-underline hover:bg-slate-50" href={examPdfUrl(id, pid, "paper", true)}><Download className="h-4 w-4" /> Exam paper PDF</a>
              <a role="menuitem" className="flex items-center gap-2 px-3 py-2 text-slate-700 no-underline hover:bg-slate-50" href={examPdfUrl(id, pid, "answers", true)}><Download className="h-4 w-4" /> Answer key PDF</a>
            </div>
          )}
        </div>
      </div>

      <Segmented label="View" value={view} onChange={setView} options={[
        { value: "practice", label: "Practice" }, { value: "paper", label: "Paper" }, { value: "answers", label: "Answer key" },
      ]} />
      {view === "practice" ? (
        <section className="grid gap-6 md:grid-cols-[1fr_260px]">
          <div className="space-y-4 border border-[var(--color-border)] bg-white p-6">
            <p className="text-sm text-slate-500">
              {paper.questions} questions · {paper.total} points{paper.duration ? ` · ${paper.duration} minutes on paper` : ""}
            </p>
            <ul className="space-y-1.5 text-sm">
              {paper.sections.map((s) => (
                <li key={s.title} className="flex justify-between gap-3 border-b border-dashed border-[var(--color-border)] pb-1.5">
                  <span>{s.title}</span>
                  <span className="text-slate-400">{s.questions.length} · {sum(s.questions.map((q) => q.points))} pts</span>
                </li>
              ))}
            </ul>
            <div className="flex flex-wrap items-center gap-3 pt-2">
              {saved ? (
                <>
                  <button type="button" className="inline-flex items-center gap-2 rounded-xl bg-emerald-500 px-8 py-3 font-semibold text-white shadow-[0_4px_0_0_#059669] hover:bg-emerald-600"
                    onClick={() => { setResume(saved); setPractising(saved.only); }}>
                    <Play className="h-4 w-4" /> Continue
                  </button>
                  <span className="text-sm text-slate-500">
                    {Object.keys(saved.results).length} of {saved.only?.length ?? paper.questions} answered
                  </span>
                  <button type="button" className="btn-secondary" onClick={() => { forgetRun(id, pid); setSaved(null); setPractising(null); }}>
                    Start over
                  </button>
                </>
              ) : (
                <button type="button" className="inline-flex items-center gap-2 rounded-xl bg-emerald-500 px-8 py-3 font-semibold text-white shadow-[0_4px_0_0_#059669] hover:bg-emerald-600"
                  onClick={() => setPractising(null)}>
                  <Play className="h-4 w-4" /> Start
                </button>
              )}
              {lastWrong.length > 0 && !saved && (
                <button type="button" className="btn-secondary" onClick={() => setPractising(lastWrong)}>
                  Retry the {lastWrong.length} I missed
                </button>
              )}
            </div>
            <p className="text-xs text-slate-400">Choice, true/false and fill-in questions are marked at once; written answers are marked by the AI the way this exam marks, or by you.</p>
          </div>
          <aside className="space-y-3 border border-[var(--color-border)] bg-white p-5 text-sm">
            <p className="flex items-center gap-2 text-slate-500"><Trophy className="h-4 w-4 text-amber-500" /> Attempts</p>
            {paper.attempts.length === 0 ? <p className="text-xs text-slate-400">None yet.</p> : (
              <>
                {best !== null && <p className="text-2xl font-semibold text-[var(--color-heading)]">{best}%<span className="ml-2 text-xs font-normal text-slate-400">best</span></p>}
                <ol className="space-y-1 text-xs text-slate-500">
                  {[...paper.attempts].reverse().slice(0, 8).map((a) => (
                    <li key={a.at} className="flex justify-between"><span>{fmtDay(a.at * 1000)}</span><span className="font-mono">{a.score} / {a.total}</span></li>
                  ))}
                </ol>
              </>
            )}
          </aside>
        </section>
      ) : (
        <section className="h-[75vh] border border-[var(--color-border)] bg-white">
          <PdfScroll key={view} file={examPdfUrl(id, pid, view)} label={view === "paper" ? "Exam paper" : "Answer key"} />
        </section>
      )}
    </div>
  );
}
