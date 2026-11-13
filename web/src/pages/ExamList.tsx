import { useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { FileUp, MoreVertical, Plus, Trash2, X } from "lucide-react";
import { createExam, deleteGeneration, listGenerations, type QuestionType, type SpecRow } from "../api";
import { prepareFigure } from "../imageTools";
import { modelReady, useModel } from "../model";
import Segmented from "../components/Segmented";
import StatusBadge from "../components/StatusBadge";
import { displayTitle } from "../title";
import type { GenerationMeta, SlideLanguage } from "../types";

// Learn → Practice exam: the reader's exams, and the form for a new one.

export const TYPE_LABELS: Record<QuestionType, string> = {
  single: "Single choice", multiple: "Multiple choice", truefalse: "True or false", fill: "Fill in the blanks",
  short: "Short answer", calc: "Calculation", other: "Other (describe)",
};
const OPEN: QuestionType[] = ["short", "calc", "other"];
const ORDER: QuestionType[] = ["single", "multiple", "truefalse", "fill", "short", "calc", "other"];
const DEFAULT_POINTS: Record<QuestionType, number> = { single: 2, multiple: 3, truefalse: 1, fill: 2, short: 8, calc: 10, other: 5 };
const MAX_SAMPLES = 6;

type Row = SpecRow & { each: boolean; items: { points: number; parts: string }[]; description: string };

const freshRows = (): Row[] => ORDER.map((type) => ({
  type, count: 0, points: DEFAULT_POINTS[type], each: false, items: [], description: "",
}));

const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));
const fmtDay = (iso: string) => new Date(iso).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });

function rowTotals(rows: Row[]) {
  let questions = 0;
  let points = 0;
  for (const r of rows) {
    if (OPEN.includes(r.type) && r.each) {
      questions += r.items.length;
      points += r.items.reduce((s, it) => s + (Number(it.points) || 0), 0);
    } else {
      questions += r.count;
      points += r.count * (Number(r.points) || 0);
    }
  }
  return { questions, points };
}

function NewExam({ onClose }: { onClose: () => void }) {
  const navigate = useNavigate();
  const { status: model, setPanelOpen } = useModel();
  const ready = model === null || modelReady(model);
  const [title, setTitle] = useState("");
  const [language, setLanguage] = useState<SlideLanguage>("source");
  const [sourceTab, setSourceTab] = useState<"pdf" | "text">("pdf");
  const [files, setFiles] = useState<File[]>([]);
  const [sourceText, setSourceText] = useState("");
  const [rows, setRows] = useState<Row[]>(freshRows);
  const [samples, setSamples] = useState<File[]>([]);
  const [duration, setDuration] = useState("");
  const [extra, setExtra] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const set = (i: number, patch: Partial<Row>) => setRows((rs) => rs.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const totals = rowTotals(rows);

  async function addSamples(list: FileList | null) {
    if (!list) return;
    setError(null);
    const picked = Array.from(list).slice(0, MAX_SAMPLES - samples.length);
    const ready: File[] = [];
    for (const f of picked) {
      if (f.type === "application/pdf" || f.name.toLowerCase().endsWith(".pdf")) ready.push(f);
      else {
        try {
          ready.push(await prepareFigure(f));
        } catch (e) {
          setError(`${f.name}: ${errMsg(e)}`);
        }
      }
    }
    setSamples((s) => [...s, ...ready].slice(0, MAX_SAMPLES));
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (!ready) return setPanelOpen(true);
    if (sourceTab === "pdf" && !files.length) return setError("Add the material: at least one PDF");
    if (sourceTab === "text" && !sourceText.trim()) return setError("Paste the material");
    if (!totals.questions && !samples.length) return setError("Choose some questions, or add a sample paper");
    const other = rows.find((r) => r.type === "other");
    if (other && (other.count > 0 || (other.each && other.items.length)) && !other.description.trim()) {
      return setError("Describe the \"Other\" questions");
    }
    setSubmitting(true);
    try {
      const { id } = await createExam({
        title: title.trim(), language, samples,
        files: sourceTab === "pdf" ? files : undefined,
        source_text: sourceTab === "text" ? sourceText : undefined,
        spec: {
          duration: Number(duration) || 0, extra: extra.trim(),
          types: rows.map((r) => ({
            type: r.type, count: OPEN.includes(r.type) && r.each ? r.items.length : r.count, points: Number(r.points) || 0,
            items: OPEN.includes(r.type) && r.each ? r.items : [], description: r.description.trim(),
          })),
        },
      });
      navigate(`/exam/${id}`);
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setSubmitting(false);
    }
  }

  const num = "input-field w-16 px-2 py-1 text-center text-sm";
  return (
    <div className="fixed inset-0 z-40 flex items-start justify-center overflow-y-auto bg-slate-900/30 p-4 sm:p-10"
      onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <section className="w-full max-w-3xl border border-[var(--color-border)] bg-white p-6 shadow-xl">
        <div className="mb-6 flex items-center justify-between gap-3">
          <h1 className="text-2xl">New practice exam</h1>
          <button type="button" className="p-1 text-slate-500 hover:text-slate-800" aria-label="Close" onClick={onClose}>
            <X className="h-5 w-5" />
          </button>
        </div>
        <form onSubmit={submit} className="space-y-6">
          <div>
            <div className="mb-2 flex items-center justify-between gap-3">
              <span className="text-sm text-slate-600">Material the questions come from</span>
              <Segmented label="Material" value={sourceTab} onChange={setSourceTab}
                options={[{ value: "pdf", label: "PDF" }, { value: "text", label: "Paste text" }]} />
            </div>
            {sourceTab === "pdf" ? (
              <>
                <label className="flex cursor-pointer flex-col items-center border border-dashed border-[var(--color-border)] bg-slate-50 px-4 py-6 text-sm text-slate-600"
                  onDragOver={(e) => e.preventDefault()}
                  onDrop={(e) => { e.preventDefault(); setFiles((f) => [...f, ...Array.from(e.dataTransfer.files).filter((x) => x.name.toLowerCase().endsWith(".pdf"))]); }}>
                  <FileUp className="mb-2 h-5 w-5" />
                  {files.length ? "Add another PDF" : "Click to select or drop PDFs"}
                  <input type="file" multiple accept="application/pdf,.pdf" className="sr-only"
                    onChange={(e) => { const picked = Array.from(e.target.files ?? []); setFiles((f) => [...f, ...picked]); e.target.value = ""; }} />
                </label>
                {files.length > 0 && (
                  <ul className="mt-2 space-y-1">
                    {files.map((f, i) => (
                      <li key={`${f.name}-${i}`} className="flex items-center gap-2 border border-[var(--color-border)] px-2 py-1 text-sm">
                        <span className="min-w-0 flex-1 truncate">{f.name}</span>
                        <button type="button" className="px-1 text-red-600" aria-label={`Remove ${f.name}`}
                          onClick={() => setFiles((all) => all.filter((_, j) => j !== i))}><Trash2 className="h-3 w-3" /></button>
                      </li>
                    ))}
                  </ul>
                )}
              </>
            ) : (
              <textarea className="input-field min-h-32" value={sourceText} aria-label="Pasted material"
                onChange={(e) => setSourceText(e.target.value)} placeholder="Paste the material here…" />
            )}
          </div>

          <div>
            <div className="mb-2 flex items-baseline justify-between gap-3">
              <span className="text-sm text-slate-600">Questions</span>
              <span className="text-xs text-slate-500">{totals.questions} questions · {totals.points} points</span>
            </div>
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-slate-400">
                  <th className="py-1 font-normal">Type</th>
                  <th className="w-20 py-1 text-center font-normal">How many</th>
                  <th className="w-24 py-1 text-center font-normal">Points each</th>
                  <th className="py-1 font-normal" />
                </tr>
              </thead>
              <tbody>
                {rows.map((r, i) => {
                  const open = OPEN.includes(r.type);
                  const active = r.each ? r.items.length > 0 : r.count > 0;
                  return (
                    <tr key={r.type} className="border-t border-[var(--color-border)] align-top">
                      <td className={`py-2 ${active ? "text-[var(--color-heading)]" : "text-slate-500"}`}>
                        {TYPE_LABELS[r.type]}
                        {r.type === "other" && (active || r.description) && (
                          <textarea className="input-field mt-1.5 min-h-14 text-xs" value={r.description}
                            aria-label="Describe the other questions"
                            placeholder="What they look like, e.g. match each paragraph to a heading; reading passage with 5 questions"
                            onChange={(e) => set(i, { description: e.target.value })} />
                        )}
                        {open && r.each && (
                          <ol className="mt-1.5 space-y-1">
                            {r.items.map((it, k) => (
                              <li key={k} className="flex items-center gap-1.5 text-xs text-slate-500">
                                <span className="w-6">{k + 1}.</span>
                                <input className={num} type="number" min={0} step={0.5} aria-label={`Points of question ${k + 1}`}
                                  value={it.points} onChange={(e) => set(i, { items: r.items.map((x, j) => (j === k ? { ...x, points: Number(e.target.value) } : x)) })} />
                                points, split
                                <input className="input-field w-28 px-2 py-1 text-xs" aria-label={`Sub-questions of question ${k + 1}`}
                                  placeholder="e.g. 4+6" value={it.parts}
                                  onChange={(e) => set(i, { items: r.items.map((x, j) => (j === k ? { ...x, parts: e.target.value } : x)) })} />
                                <button type="button" className="px-1 text-slate-400 hover:text-red-600" aria-label={`Remove question ${k + 1}`}
                                  onClick={() => set(i, { items: r.items.filter((_, j) => j !== k) })}><X className="h-3.5 w-3.5" /></button>
                              </li>
                            ))}
                            <li>
                              <button type="button" className="text-xs text-[var(--color-primary)] hover:underline"
                                onClick={() => set(i, { items: [...r.items, { points: Number(r.points) || DEFAULT_POINTS[r.type], parts: "" }] })}>
                                + Add a question
                              </button>
                            </li>
                          </ol>
                        )}
                      </td>
                      <td className="py-2 text-center">
                        {!r.each && <input className={num} type="number" min={0} max={50} aria-label={`${TYPE_LABELS[r.type]}: how many`}
                          value={r.count} onChange={(e) => set(i, { count: Math.max(0, Math.min(50, Number(e.target.value) || 0)) })} />}
                      </td>
                      <td className="py-2 text-center">
                        {!r.each && <input className={num} type="number" min={0} step={0.5} aria-label={`${TYPE_LABELS[r.type]}: points each`}
                          value={r.points} onChange={(e) => set(i, { points: Number(e.target.value) })} />}
                      </td>
                      <td className="py-2 text-right">
                        {open && (
                          <button type="button" className="text-xs text-slate-500 hover:text-[var(--color-primary)]"
                            title="Give each question its own points and sub-question split"
                            onClick={() => set(i, r.each ? { each: false, count: r.items.length, items: [] }
                              : { each: true, items: Array.from({ length: Math.max(1, r.count) }, () => ({ points: Number(r.points) || 0, parts: "" })) })}>
                            {r.each ? "Same for all" : "Set each"}
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          <div>
            <span className="mb-2 block text-sm text-slate-600">Sample papers <span className="text-xs text-slate-400">(optional: photos or PDFs; the exam follows their style, and fills in what the table leaves open)</span></span>
            <label className="flex cursor-pointer items-center justify-center gap-2 border border-dashed border-[var(--color-border)] bg-slate-50 px-4 py-4 text-sm text-slate-600">
              <FileUp className="h-4 w-4" /> {samples.length ? "Add another sample" : "Add a sample paper"}
              <input type="file" multiple accept="image/*,.heic,.heif,application/pdf,.pdf" className="sr-only"
                onChange={(e) => { void addSamples(e.target.files); e.target.value = ""; }} />
            </label>
            {samples.length > 0 && (
              <ul className="mt-2 flex flex-wrap gap-2">
                {samples.map((f, i) => (
                  <li key={`${f.name}-${i}`} className="flex items-center gap-1.5 border border-[var(--color-border)] px-2 py-1 text-xs">
                    {f.name}
                    <button type="button" className="text-slate-400 hover:text-red-600" aria-label={`Remove ${f.name}`}
                      onClick={() => setSamples((s) => s.filter((_, j) => j !== i))}><X className="h-3 w-3" /></button>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="grid gap-4 sm:grid-cols-[1fr_auto]">
            <label className="block text-sm text-slate-600">Title <span className="text-xs text-slate-400">(optional)</span>
              <input className="input-field mt-1" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Made from the material if empty" />
            </label>
            <label className="block text-sm text-slate-600">Time, minutes
              <input className="input-field mt-1 w-28" type="number" min={0} value={duration} onChange={(e) => setDuration(e.target.value)} placeholder="none" />
            </label>
          </div>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <span className="text-sm text-slate-600">Language</span>
            <Segmented label="Language" value={language} onChange={setLanguage} options={[
              { value: "source", label: "As the material" }, { value: "en", label: "English" }, { value: "zh", label: "中文" },
            ]} />
          </div>
          <label className="block text-sm text-slate-600">Anything else <span className="text-xs text-slate-400">(optional)</span>
            <textarea className="input-field mt-1 min-h-16" value={extra} onChange={(e) => setExtra(e.target.value)}
              placeholder="e.g. focus on chapters 3 and 4; harder than the sample; no proofs" />
          </label>
          {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
          <div className="flex justify-end gap-2">
            <button type="button" className="btn-secondary" onClick={onClose}>Cancel</button>
            <button type="submit" className="btn-primary" disabled={submitting}>
              {submitting ? "Uploading…" : samples.length ? "Read the samples" : "Next: check the structure"}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}

export default function ExamList() {
  const [items, setItems] = useState<GenerationMeta[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [newOpen, setNewOpen] = useState(false);
  const [menuId, setMenuId] = useState<string | null>(null);
  const [confirmId, setConfirmId] = useState<string | null>(null);

  const load = () => listGenerations().then((gens) => setItems(gens.filter((g) => g.kind === "exam")), (e) => setError(errMsg(e)));
  useEffect(() => { void load(); }, []);
  useEffect(() => {
    if (!items.some((g) => g.status === "running")) return;
    const t = window.setInterval(() => void load(), 4000);
    return () => window.clearInterval(t);
  }, [items]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") { setNewOpen(false); setMenuId(null); } };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  async function remove(id: string) {
    setMenuId(null);
    try {
      await deleteGeneration(id);
      setItems((all) => all.filter((g) => g.id !== id));
    } catch (e) {
      setError(errMsg(e));
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-baseline gap-4">
          <Link to="/learn" className="text-sm text-[var(--color-primary)] no-underline hover:underline">← Learn</Link>
          <h1 className="text-3xl">Practice exams</h1>
        </div>
        <button type="button" className="btn-primary rounded-full px-5" onClick={() => setNewOpen(true)}>
          <Plus className="h-4 w-4" /> New exam
        </button>
      </div>
      {error && <div role="alert" className="border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">{error}</div>}
      {items.length === 0 ? (
        <div className="border border-dashed border-[var(--color-border)] px-6 py-14 text-center text-sm text-slate-500">
          No exams yet. Upload your material and choose the questions you want.
        </div>
      ) : (
        <table className="w-full border border-[var(--color-border)] bg-white text-sm">
          <thead>
            <tr className="border-b border-[var(--color-border)] text-left text-xs text-slate-500">
              <th className="px-4 py-2 font-normal">Title</th>
              <th className="hidden px-2 py-2 text-center font-normal sm:table-cell">Questions</th>
              <th className="hidden px-2 py-2 text-center font-normal sm:table-cell">Created</th>
              <th className="px-2 py-2 text-center font-normal">Status</th>
              <th className="w-12" />
            </tr>
          </thead>
          <tbody>
            {items.map((g) => (
              <tr key={g.id} className="border-b border-[var(--color-border)] last:border-0 hover:bg-slate-50">
                <td className="px-4 py-3"><Link to={`/exam/${g.id}`} className="text-[var(--color-heading)] no-underline hover:underline">{displayTitle(g)}</Link></td>
                <td className="hidden text-center text-slate-600 sm:table-cell">{g.frame_count || ""}</td>
                <td className="hidden text-center text-slate-600 sm:table-cell">{fmtDay(g.created_at)}</td>
                <td className="text-center"><StatusBadge status={g.status} /></td>
                <td className="relative text-center">
                  <button type="button" className="p-1.5 text-slate-500 hover:text-slate-800" aria-label={`Actions for "${displayTitle(g)}"`}
                    onClick={() => { setMenuId((m) => (m === g.id ? null : g.id)); setConfirmId(null); }}>
                    <MoreVertical className="h-4 w-4" />
                  </button>
                  {menuId === g.id && <div className="fixed inset-0 z-10" onClick={() => setMenuId(null)} />}
                  {menuId === g.id && (
                    <div className="absolute right-2 top-full z-20 w-40 border border-[var(--color-border)] bg-white py-1 text-left shadow-lg">
                      <button type="button" className="flex w-full items-center gap-2 px-3 py-2 text-red-600 hover:bg-red-50"
                        disabled={g.status === "running"}
                        onClick={() => (confirmId === g.id ? void remove(g.id) : setConfirmId(g.id))}>
                        <Trash2 className="h-4 w-4" /> {confirmId === g.id ? "Confirm delete" : "Delete"}
                      </button>
                    </div>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {newOpen && <NewExam onClose={() => setNewOpen(false)} />}
    </div>
  );
}
