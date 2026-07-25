import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import { Download, FileText, Package } from "lucide-react";
import {
  bundleUrl,
  editFrame,
  getFrames,
  getGeneration,
  logUrl,
  pdfUrl,
  texUrl,
} from "../api";
import PdfViewer from "../components/PdfViewer";
import StatusBadge from "../components/StatusBadge";
import { useTaskWs } from "../hooks/useTaskWs";
import type { FrameItem, GenerationMeta, TaskEvent } from "../types";

const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));

export default function Generation() {
  const { id = "" } = useParams();
  const [meta, setMeta] = useState<GenerationMeta | null>(null);
  const [frames, setFrames] = useState<FrameItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [pdfBust, setPdfBust] = useState(0);
  const [editText, setEditText] = useState("");
  const [editError, setEditError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [progress, setProgress] = useState<TaskEvent | null>(null);

  const loadMeta = useCallback(async () => {
    if (!id) return;
    const data = await getGeneration(id);
    setMeta(data);
    return data;
  }, [id]);

  const loadFrames = useCallback(async () => {
    if (!id) return;
    setFrames(await getFrames(id));
  }, [id]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const data = await loadMeta();
        if (cancelled || !data) return;
        if (data.status === "succeeded") await loadFrames();
        setError(null);
      } catch (e) {
        if (!cancelled) setError(errMsg(e));
      }
    })();
    return () => { cancelled = true; };
  }, [loadMeta, loadFrames]);

  const watchWs = Boolean(id) && (meta?.status === "running" || editing || progress?.status === "running");

  useTaskWs(id, watchWs, (event) => {
    setProgress(event);
    if (event.status === "running") return;
    void (async () => {
      try {
        await loadMeta();
        if (event.status === "succeeded") {
          await loadFrames();
          setPdfBust(Date.now());
          setEditText("");
        }
        setError(null);
      } catch (e) {
        setError(errMsg(e));
      } finally {
        setEditing(false);
      }
    })();
  });

  async function handleEdit(e: FormEvent) {
    e.preventDefault();
    if (!id || !editText.trim()) return setEditError("Please enter edit instructions");
    setEditError(null);
    setEditing(true);
    setProgress({ gen_id: id, status: "running", percent: 0, stage: "Submitting edit…", error: "" });
    try {
      await editFrame(id, page, editText.trim());
    } catch (err) {
      setEditing(false);
      setProgress(null);
      setEditError(errMsg(err));
    }
  }

  if (error && !meta) {
    return (
      <div className="border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
        {error}
        <div className="mt-3"><Link to="/" className="underline">Back to home</Link></div>
      </div>
    );
  }
  if (!meta) return <p className="text-sm text-slate-500">Loading…</p>;

  const frame = frames.find((f) => f.index === page);
  const showProgress = meta.status === "running" || editing || progress?.status === "running";
  const failed = meta.status === "failed" && !editing;
  const succeeded = meta.status === "succeeded" && !editing;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0 space-y-2">
          <div className="flex flex-wrap items-center gap-3">
            <h1 className="truncate text-2xl">{meta.title}</h1>
            <StatusBadge status={editing ? "running" : meta.status} />
          </div>
          <p className="text-sm text-slate-500">
            Template: {meta.template_name || meta.template_key}
            {meta.author?.trim() ? ` · Author: ${meta.author}` : ""}
            {meta.institute?.trim() ? ` · Institute: ${meta.institute}` : ""}
            {meta.frame_count > 0 ? ` · ${meta.frame_count} pages` : ""}
          </p>
        </div>
        {succeeded && (
          <div className="flex flex-wrap gap-2">
            <a className="btn-secondary text-sm" href={pdfUrl(id)} download>
              <Download className="h-4 w-4" /> PDF
            </a>
            <a className="btn-secondary text-sm" href={texUrl(id)} download="main.tex">
              <FileText className="h-4 w-4" /> main.tex
            </a>
            <a className="btn-secondary text-sm" href={bundleUrl(id)} download>
              <Package className="h-4 w-4" /> Overleaf zip
            </a>
          </div>
        )}
      </div>

      {showProgress && (
        <section className="app-surface space-y-3 p-6">
          <div className="flex items-center justify-between text-sm">
            <span>{progress?.stage || "Working…"}</span>
            <span className="font-mono text-slate-500">{progress?.percent ?? 0}%</span>
          </div>
          <div className="h-2 w-full overflow-hidden bg-slate-100">
            <div className="h-full bg-[var(--color-primary)] transition-all duration-300"
              style={{ width: `${Math.min(100, Math.max(0, progress?.percent ?? 0))}%` }} />
          </div>
        </section>
      )}

      {failed && (
        <section className="space-y-3 border border-red-300 bg-red-50 p-6">
          <h2 className="text-lg text-red-800">Generation failed</h2>
          <pre className="whitespace-pre-wrap break-words font-mono text-sm text-red-700">
            {meta.error || progress?.error || "Unknown error"}
          </pre>
          <a href={logUrl(id)} target="_blank" rel="noreferrer" className="text-sm text-red-800 underline">
            View compile log
          </a>
        </section>
      )}

      {succeeded && (
        <div className="grid min-h-[70vh] gap-4 lg:grid-cols-[1fr_320px]">
          <section className="app-surface flex min-h-[60vh] flex-col p-3">
            <PdfViewer file={pdfUrl(id, pdfBust)} pageWidth={620} onPageChange={setPage} />
          </section>
          <aside className="app-surface flex flex-col gap-4 p-4">
            <div>
              <p className="text-xs text-slate-400">Current page</p>
              <h2 className="mt-1 text-lg">
                Page {page}{frame?.title ? ` · ${frame.title}` : ""}
              </h2>
            </div>
            {frame?.tex && (
              <div className="min-h-0 flex-1 overflow-auto border border-[var(--color-border)] bg-slate-50 p-3">
                <pre className="whitespace-pre-wrap break-all font-mono text-xs text-slate-600">{frame.tex}</pre>
              </div>
            )}
            <form onSubmit={handleEdit} className="space-y-3 border-t border-[var(--color-border)] pt-4">
              <label className="block text-sm text-slate-600" htmlFor="edit-instruction">Edit this page</label>
              <textarea id="edit-instruction" className="input-field min-h-24 text-sm"
                value={editText} onChange={(e) => setEditText(e.target.value)}
                placeholder="e.g. Change the bar chart to a line chart" disabled={editing} />
              {editError && (
                <div className="border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{editError}</div>
              )}
              <button type="submit" className="btn-primary w-full" disabled={editing}>
                {editing ? "Editing…" : "Submit edit"}
              </button>
            </form>
          </aside>
        </div>
      )}

      <Link to="/" className="text-sm text-[var(--color-primary)] underline">← Back to home</Link>
    </div>
  );
}
