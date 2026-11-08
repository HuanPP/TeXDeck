import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Download, FileText, Package, Presentation, RotateCw } from "lucide-react";
import {
  bundleUrl,
  editFrame,
  editSourceText,
  getFrames,
  getGeneration,
  locateText,
  logUrl,
  pdfUrl,
  pptxUrl,
  refineGeneration,
  regenerateGeneration,
  texUrl,
} from "../api";
import PdfViewer, { type TextClick } from "../components/PdfViewer";
import QuickEdit, { type QuickEditState } from "../components/QuickEdit";
import Segmented from "../components/Segmented";
import StatusBadge from "../components/StatusBadge";
import { useModel } from "../model";
import { displayTitle } from "../title";
import { useTaskWs } from "../hooks/useTaskWs";
import type { FrameItem, TaskEvent, GenerationMeta } from "../types";

const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));
const fmtTokens = (n?: number) => {
  const v = n ?? 0;
  return v >= 1000 ? `${Math.round(v / 1000)}k` : String(v);
};

export default function Generation() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const [meta, setMeta] = useState<GenerationMeta | null>(null);
  const [frames, setFrames] = useState<FrameItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [pdfBust, setPdfBust] = useState(0);
  const [editText, setEditText] = useState("");
  const [editError, setEditError] = useState<string | null>(null);
  const [scope, setScope] = useState<"page" | "deck">("page");
  const [texOpen, setTexOpen] = useState(false);
  const [regenerating, setRegenerating] = useState(false);
  const [editing, setEditing] = useState(false);
  const [progress, setProgress] = useState<TaskEvent | null>(null);
  // Attach the WS on mount: a reload during a running generation or edit then
  // resumes progress (the server re-sends the current task state on connect).
  const [watch, setWatch] = useState(true);
  const [errorDismissed, setErrorDismissed] = useState(false);
  const [logOpen, setLogOpen] = useState(false);
  const [logText, setLogText] = useState<string | null>(null);
  const [logLoading, setLogLoading] = useState(false);
  const sawRunningRef = useRef(false);
  const { setPanelOpen } = useModel();

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

  const metaStatus = meta?.status;
  const metaUpdated = meta?.updated_at;
  const progressRunning = progress?.status === "running";
  const busy = editing || progressRunning;

  // Click-to-edit: text clicked on the preview opens an editor for it.
  const [quick, setQuick] = useState<QuickEditState | null>(null);
  const quickSeq = useRef(0);
  const closeQuick = useCallback(() => setQuick(null), []);

  function handleTextClick({ text, page: clickedPage, rect }: TextClick) {
    if (!id || busy) return;
    const seq = ++quickSeq.current;
    setQuick({ x: rect.left + window.scrollX, y: rect.bottom + window.scrollY, status: "locating", value: "" });
    locateText(id, clickedPage, text).then(
      (hit) => {
        if (seq !== quickSeq.current) return;
        setQuick((q) => q && { ...q, status: "ready", hit, value: hit.text });
      },
      (err) => {
        if (seq !== quickSeq.current) return;
        setQuick((q) => q && { ...q, status: "ready", error: `${errMsg(err)}. Describe the change under This page instead.` });
      },
    );
  }

  async function saveQuick() {
    if (!id || !quick?.hit) return;
    setQuick({ ...quick, status: "saving", error: undefined });
    try {
      await editSourceText(id, quick.hit, quick.value);
      setQuick(null);
      setEditError(null);
      setEditing(true);
      setProgress({ gen_id: id, status: "running", percent: 0, stage: "Saving your change…", error: "" });
      setWatch(true);
    } catch (err) {
      setQuick((q) => q && { ...q, status: "ready", error: errMsg(err) });
    }
  }

  const onWsReconnect = useCallback(() => {
    // The server re-sends the task state itself on reconnect; refetch meta in
    // case it changed (e.g. the task finished) while we were offline.
    void loadMeta().catch(() => undefined);
  }, [loadMeta]);

  useTaskWs(id, Boolean(id) && watch, (event) => {
    setProgress(event);
    if (event.status === "running") {
      sawRunningRef.current = true;
      return;
    }
    // Terminal event. It may also be a stale snapshot of an old task that the
    // server re-sends on connect, so only refresh the PDF/frames when we
    // actually watched this task run (or meta demonstrably changed).
    const wasActive = sawRunningRef.current || editing;
    sawRunningRef.current = false;
    setWatch(false);
    void (async () => {
      try {
        const fresh = await loadMeta();
        const changed = Boolean(fresh && metaUpdated && fresh.updated_at !== metaUpdated);
        if (event.status === "succeeded" && (wasActive || changed)) {
          await loadFrames();
          setPdfBust(Date.now());
          setEditText("");
        }
        if (wasActive || changed) {
          setLogText(null);
          setLogOpen(false);
          setErrorDismissed(false);
        }
        setError(null);
      } catch (e) {
        setError(errMsg(e));
      } finally {
        setEditing(false);
      }
    })();
  }, onWsReconnect);

  // Fallback poll while a task runs, so a dead socket or restarted server
  // cannot freeze the page forever.
  useEffect(() => {
    if (!id || !(metaStatus === "running" || editing || progressRunning)) return;
    const timer = window.setInterval(() => {
      void (async () => {
        try {
          const fresh = await loadMeta();
          if (!fresh) return;
          if (metaStatus === "running") {
            if (fresh.status === "running") return;
            if (fresh.status === "succeeded") {
              await loadFrames();
              setPdfBust(Date.now());
            }
          } else if (!metaUpdated || fresh.updated_at === metaUpdated) {
            return; // edit still running: meta is only rewritten when it finishes
          } else if (!(fresh.error || "").trim()) {
            await loadFrames();
            setPdfBust(Date.now());
            setEditText("");
          }
          setLogText(null);
          setLogOpen(false);
          setErrorDismissed(false);
          setProgress(null);
          setEditing(false);
          setWatch(false);
        } catch {
          /* transient poll failure: retry on the next tick */
        }
      })();
    }, 10_000);
    return () => window.clearInterval(timer);
  }, [id, metaStatus, metaUpdated, editing, progressRunning, loadMeta, loadFrames]);

  // The frames API returns a synctex-resolved PDF `page` per frame (index and
  // page diverge with section splash pages, \pause, allowframebreaks…).
  // `page` maps the viewer page to a frame; `index` is the key for the edit API.
  const frame = frames.find((f) => f.page === page);

  async function handleEdit(e: FormEvent) {
    e.preventDefault();
    if (!id || !editText.trim()) return setEditError("Please describe the change");
    if (!frame) return setEditError("This page has no editable slide");
    setEditError(null);
    setEditing(true);
    setProgress({ gen_id: id, status: "running", percent: 0, stage: "Submitting edit…", error: "" });
    try {
      await editFrame(id, frame.index, editText.trim());
      // The task state is registered before the 202 returns, so (re)attaching
      // the WS now cannot pick up a stale snapshot of the previous task.
      setWatch(true);
    } catch (err) {
      setEditing(false);
      setProgress(null);
      setEditError(errMsg(err));
    }
  }

  async function handleRefine(e: FormEvent) {
    e.preventDefault();
    if (!id || !editText.trim()) return setEditError("Please describe the change");
    setEditError(null);
    setEditing(true);
    setProgress({ gen_id: id, status: "running", percent: 0, stage: "Submitting refine…", error: "" });
    try {
      await refineGeneration(id, editText.trim());
      // Same flow as slide edits: the task state is registered before the 202
      // returns, so re-attaching the WS now cannot pick up a stale snapshot.
      setWatch(true);
    } catch (err) {
      setEditing(false);
      setProgress(null);
      setEditError(errMsg(err));
    }
  }

  async function handleRegenerate() {
    if (!id || regenerating) return;
    setRegenerating(true);
    try {
      const result = await regenerateGeneration(id);
      navigate(`/g/${result.id}`);
    } catch (err) {
      setError(errMsg(err));
      setRegenerating(false);
    }
  }

  async function toggleLog() {
    if (logOpen) return setLogOpen(false);
    setLogOpen(true);
    if (logText !== null || logLoading) return;
    setLogLoading(true);
    try {
      const res = await fetch(logUrl(id));
      if (res.status === 404) {
        setLogText("No compile log — the failure happened before compilation.");
      } else if (!res.ok) {
        setLogText(`Failed to load compile log (HTTP ${res.status})`);
      } else {
        setLogText(await res.text());
      }
    } catch (err) {
      setLogText(`Failed to load compile log: ${errMsg(err)}`);
    } finally {
      setLogLoading(false);
    }
  }

  if (error && !meta) {
    return (
      <div role="alert" className="border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
        {error}
        <div className="mt-3"><Link to="/" className="underline">Back to home</Link></div>
      </div>
    );
  }
  if (!meta) return <p className="text-sm text-slate-500">Loading…</p>;

  const showProgress = meta.status === "running" || busy;
  const failed = meta.status === "failed" && !busy;
  const succeeded = meta.status === "succeeded";
  const percent = Math.min(100, Math.max(0, progress?.percent ?? 0));
  const editFailedError = succeeded && !busy && !errorDismissed ? (meta.error || "").trim() : "";
  // Refine failures roll back like edits but carry their own message prefix.
  const refineFailed = editFailedError.startsWith("This refine failed");
  const llmCalls = meta.llm_calls ?? 0;
  // Model failures (signed out, quota, model unavailable) need a different
  // model or a retry, and have no compile log to show.
  const modelError = meta.error_kind === "model";
  const changeModel = (
    <button type="button" className="btn-secondary text-sm" onClick={() => setPanelOpen(true)}>
      Change model
    </button>
  );

  const logBlock = (tone: string) => (
    <div className="space-y-2">
      <button type="button" onClick={() => void toggleLog()} aria-expanded={logOpen}
        className={`text-sm underline ${tone}`}>
        {logOpen ? "Hide compile log" : "View compile log"}
      </button>
      {logOpen && (
        <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words border border-[var(--color-border)] bg-white p-3 font-mono text-xs text-slate-600">
          {logLoading ? "Loading log…" : logText}
        </pre>
      )}
    </div>
  );

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0 space-y-2">
          <div className="flex flex-wrap items-center gap-3">
            <h1 className="truncate text-2xl">{displayTitle(meta)}</h1>
            <StatusBadge status={busy ? "running" : meta.status} />
          </div>
          <p className="text-sm text-slate-500">
            Template: {meta.template_name || meta.template_key}
            {meta.author?.trim() ? ` · Author: ${meta.author}` : ""}
            {meta.institute?.trim() ? ` · Institute: ${meta.institute}` : ""}
            {meta.frame_count > 0 ? ` · ${meta.frame_count} pages` : ""}
          </p>
          {meta.source_names.length > 0 && (
            <p className="truncate text-xs text-slate-400" title={meta.source_names.join(", ")}>
              Sources: {meta.source_names.join(", ")}
            </p>
          )}
          {llmCalls > 0 && (
            <p className="text-xs text-slate-400">
              {meta.llm_model || "LLM"} · {llmCalls} {llmCalls === 1 ? "call" : "calls"} ·{" "}
              {fmtTokens(meta.llm_prompt_tokens)} in / {fmtTokens(meta.llm_completion_tokens)} out
            </p>
          )}
        </div>
        <div className="flex flex-wrap gap-2">
          {succeeded && (
            <>
              <a className="btn-secondary text-sm" href={pdfUrl(id)} download>
                <Download className="h-4 w-4" /> PDF
              </a>
              <a className="btn-secondary text-sm" href={pptxUrl(id)} download
                title="Editable PowerPoint: text boxes over the PDF's own graphics">
                <Presentation className="h-4 w-4" /> PPTX
              </a>
              <a className="btn-secondary text-sm" href={texUrl(id)} download="main.tex">
                <FileText className="h-4 w-4" /> main.tex
              </a>
              <a className="btn-secondary text-sm" href={bundleUrl(id)} download>
                <Package className="h-4 w-4" /> Overleaf zip
              </a>
            </>
          )}
          {!(failed && modelError) && (
            <button type="button" className="btn-secondary text-sm"
              disabled={busy || meta.status === "running" || regenerating}
              onClick={() => void handleRegenerate()}>
              <RotateCw className={`h-4 w-4 ${regenerating ? "animate-spin" : ""}`} />
              {regenerating ? "Regenerating…" : "Regenerate"}
            </button>
          )}
        </div>
      </div>

      {error && (
        <div role="alert" className="border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          {error}
        </div>
      )}

      {showProgress && (
        <section className="app-surface space-y-3 p-6">
          <div className="flex items-center justify-between text-sm">
            <span aria-live="polite">{progress?.stage || "Working…"}</span>
            <span className="font-mono text-slate-500">{percent}%</span>
          </div>
          <div role="progressbar" aria-label="Task progress"
            aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent}
            className="h-2 w-full overflow-hidden bg-slate-100">
            <div className="h-full bg-[var(--color-primary)] transition-all duration-300"
              style={{ width: `${percent}%` }} />
          </div>
        </section>
      )}

      {editFailedError && (
        <section role="alert" className="space-y-3 border border-red-300 bg-red-50 p-6">
          <div className="flex items-start justify-between gap-3">
            <h2 className="text-lg text-red-800">
              {refineFailed
                ? "Refine failed — your deck was not changed"
                : "Edit failed — your deck was not changed"}
            </h2>
            <button type="button" className="text-sm text-red-800 underline"
              onClick={() => setErrorDismissed(true)}>
              Dismiss
            </button>
          </div>
          <pre className="whitespace-pre-wrap break-words font-mono text-sm text-red-700">{editFailedError}</pre>
          {modelError ? <div>{changeModel}</div> : logBlock("text-red-800")}
        </section>
      )}

      {failed && (
        <section role="alert" className="space-y-3 border border-red-300 bg-red-50 p-6">
          <h2 className="text-lg text-red-800">Generation failed</h2>
          <pre className="whitespace-pre-wrap break-words font-mono text-sm text-red-700">
            {meta.error || progress?.error || "Unknown error"}
          </pre>
          {modelError ? (
            <div className="flex flex-wrap gap-2">
              {changeModel}
              <button type="button" className="btn-primary text-sm" disabled={regenerating}
                onClick={() => void handleRegenerate()}>
                <RotateCw className={`h-4 w-4 ${regenerating ? "animate-spin" : ""}`} /> Try again
              </button>
            </div>
          ) : logBlock("text-red-800")}
        </section>
      )}

      {succeeded && (
        <div className="grid min-h-[70vh] gap-4 lg:grid-cols-[1fr_320px]">
          <section className="app-surface flex min-h-[60vh] min-w-0 flex-col p-3">
            <PdfViewer file={pdfUrl(id, pdfBust)} onPageChange={setPage}
              onTextClick={busy ? undefined : handleTextClick} />
          </section>
          <aside className="app-surface flex flex-col gap-4 p-4">
            <div>
              <p className="text-xs text-slate-500">Current page</p>
              <h2 className="mt-1 text-lg">
                Page {page}{frame?.title ? ` · ${frame.title}` : ""}
              </h2>
              {frame?.tex && (
                <button type="button" className="mt-1 text-xs text-[var(--color-primary)] hover:underline"
                  aria-expanded={texOpen} onClick={() => setTexOpen((o) => !o)}>
                  {texOpen ? "Hide LaTeX" : "Show LaTeX"}
                </button>
              )}
            </div>
            {texOpen && frame?.tex && (
              <div className="max-h-72 min-h-0 overflow-auto border border-[var(--color-border)] bg-slate-50 p-3">
                <pre className="whitespace-pre-wrap break-all font-mono text-xs text-slate-600">{frame.tex}</pre>
              </div>
            )}
            {frame && (
              <div className="border-t border-[var(--color-border)] pt-4">
                <p className="text-xs text-slate-500">Speaker notes</p>
                {frame.notes ? (
                  <p className="mt-2 max-h-60 overflow-auto whitespace-pre-wrap text-sm leading-relaxed text-slate-700">
                    {frame.notes}
                  </p>
                ) : (
                  <p className="mt-2 text-xs leading-relaxed text-slate-500">
                    No notes for this slide. Ask for them below (e.g. "Write speaker notes for this
                    slide"), or add them to every slide with Whole deck. Notes also go into the PPTX.
                  </p>
                )}
              </div>
            )}
            <form onSubmit={scope === "page" ? handleEdit : handleRefine}
              className="space-y-3 border-t border-[var(--color-border)] pt-4">
              <div className="flex items-center justify-between gap-2">
                <label className="text-sm text-slate-600" htmlFor="change-instruction">Change</label>
                <Segmented label="What to change" value={scope} onChange={setScope} disabled={busy} options={[
                  { value: "page", label: "This page" },
                  { value: "deck", label: "Whole deck" },
                ]} />
              </div>
              {scope === "page" && (
                <p className="text-xs text-slate-500">
                  To fix wording, click the text on the slide and edit it directly.
                </p>
              )}
              {scope === "page" && frames.length > 0 && !frame && (
                <p id="edit-no-frame-hint" className="text-xs text-slate-500">
                  This page has no editable slide (it may be an auto-generated section or
                  continuation page). Go to a content slide, or change the whole deck.
                </p>
              )}
              <textarea id="change-instruction" className="input-field min-h-28 text-sm"
                value={editText} onChange={(e) => setEditText(e.target.value)}
                placeholder={scope === "page"
                  ? "e.g. Change the bar chart to a line chart"
                  : "e.g. Tighten all bullet points to one line and unify tense"}
                disabled={busy || (scope === "page" && !frame)}
                aria-describedby={scope === "page" && frames.length > 0 && !frame ? "edit-no-frame-hint" : undefined} />
              {editError && (
                <div role="alert" className="border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{editError}</div>
              )}
              <button type="submit" className="btn-primary w-full" disabled={busy || (scope === "page" && !frame)}>
                {busy ? "Working…" : scope === "page" ? "Apply to this page" : "Apply to every slide"}
              </button>
              {scope === "deck" && (
                <p className="text-xs text-slate-500">If the result does not compile, the deck is left as it was.</p>
              )}
            </form>
          </aside>
        </div>
      )}

      {quick && (
        <QuickEdit state={quick} onClose={closeQuick} onSave={() => void saveQuick()}
          onChange={(value) => setQuick((q) => q && { ...q, value, error: undefined })} />
      )}

      <Link to="/" className="text-sm text-[var(--color-primary)] underline">← Back to home</Link>
    </div>
  );
}
