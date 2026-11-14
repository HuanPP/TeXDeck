import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ChevronDown, Download, FileText, ImagePlus, Package, Presentation, RotateCw } from "lucide-react";
import {
  addFigures,
  applyChanges,
  bundleUrl,
  getFrames,
  getGeneration,
  locateText,
  logUrl,
  pdfUrl,
  figureUrl,
  localFonts,
  pptxUrl,
  regenerateGeneration,
  texUrl,
  type DeckFigure,
  type QueuedText,
} from "../api";
import { prepareFigure } from "../imageTools";
import NotebookWorkspace from "../components/NotebookWorkspace";
import PdfViewer, { type PageBox, type PageMark, type TextClick } from "../components/PdfViewer";
import QuickEdit, { type QuickEditState } from "../components/QuickEdit";
import Segmented from "../components/Segmented";
import StatusBadge from "../components/StatusBadge";
import { useModel } from "../model";
import { displayTitle } from "../title";
import { useTaskWs } from "../hooks/useTaskWs";
import type { FrameItem, TaskEvent, GenerationMeta } from "../types";

const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));

// Changes wait in the browser (per deck, across reloads) until applied together.
// Images attached to a request are uploaded when it is queued (DeckFigure records).
type PendingText = QueuedText & { page: number; box: PageBox; shown: string };
// `shown` replaces the instruction in the list when it was written for the
// model (a chat answer to work in), not by the user.
type PendingPage = { index: number; instruction: string; figures: DeckFigure[]; page: number; title: string; shown?: string };
type PendingDeck = { instruction: string; figures: DeckFigure[]; shown?: string };
type Pending = { texts: PendingText[]; pages: PendingPage[]; deck?: PendingDeck };
const NOTHING: Pending = { texts: [], pages: [] };
const MAX_ATTACHED = 6;
const pendingKey = (id: string) => `texdeck.pending.${id}`;

// Just the changed part of a text fix, with a few characters around it.
function changedPart(before: string, after: string) {
  let head = 0;
  while (head < before.length && head < after.length && before[head] === after[head]) head++;
  let tail = 0;
  while (tail < before.length - head && tail < after.length - head
    && before[before.length - 1 - tail] === after[after.length - 1 - tail]) tail++;
  const CONTEXT = 8;
  return {
    lead: (head > CONTEXT ? "…" : "") + before.slice(Math.max(0, head - CONTEXT), head),
    removed: before.slice(head, before.length - tail),
    added: after.slice(head, after.length - tail),
    trail: before.slice(before.length - tail, before.length - tail + CONTEXT) + (tail > CONTEXT ? "…" : ""),
  };
}

function loadPending(id: string): Pending {
  try {
    const data = JSON.parse(localStorage.getItem(pendingKey(id)) ?? "null");
    if (!(data && Array.isArray(data.texts) && Array.isArray(data.pages))) return NOTHING;
    // Lists saved before images could be attached have no `figures`.
    const pages = data.pages.map((r: PendingPage) => ({ ...r, figures: r.figures ?? [] }));
    return { ...data, pages };
  } catch {
    return NOTHING;
  }
}
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
  const [moreOpen, setMoreOpen] = useState(false); // the notebook's Export menu
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
  const [pending, setPendingState] = useState<Pending>(() => loadPending(id));
  const applyingRef = useRef(false);
  const setPending = useCallback((next: Pending | ((p: Pending) => Pending)) => {
    setPendingState((prev) => {
      const value = typeof next === "function" ? next(prev) : next;
      if (value.texts.length || value.pages.length || value.deck) {
        localStorage.setItem(pendingKey(id), JSON.stringify(value));
      }
      else localStorage.removeItem(pendingKey(id));
      return value;
    });
  }, [id]);
  useEffect(() => setPendingState(loadPending(id)), [id]);
  const pendingCount = pending.texts.length + pending.pages.length + (pending.deck ? 1 : 0);
  // Images picked for the request being written; uploaded when it is added.
  const [attached, setAttached] = useState<{ file: File; url: string }[]>([]);
  const [uploading, setUploading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  // Installed fonts, so the PPTX names fonts this computer has.
  const [myFonts, setMyFonts] = useState<string[] | null>(null);
  useEffect(() => {
    void localFonts().then(setMyFonts);
  }, []);

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

  function handleTextClick(click: TextClick) {
    const { page: clickedPage, rect } = click;
    if (!id || busy) return;
    const seq = ++quickSeq.current;
    setQuick({
      x: rect.left + window.scrollX, y: rect.bottom + window.scrollY, status: "locating", value: "",
      page: clickedPage, box: click.box,
    });
    locateText(id, clickedPage, click).then(
      (hit) => {
        if (seq !== quickSeq.current) return;
        const queued = pending.texts.find((t) =>
          t.line === hit.line && t.source === hit.source && t.start === hit.start && t.end === hit.end);
        setQuick((q) => q && { ...q, status: "ready", hit, value: queued ? queued.text : hit.text, queued: Boolean(queued) });
      },
      (err) => {
        if (seq !== quickSeq.current) return;
        setQuick((q) => q && { ...q, status: "ready", error: `${errMsg(err)}. Describe the change under This page instead.` });
      },
    );
  }

  // Queue the fix; a new one on the same run of text replaces the old.
  function saveQuick() {
    const hit = quick?.hit;
    if (!quick || !hit || quick.page === undefined || !quick.box) return;
    const entry: PendingText = {
      line: hit.line, source: hit.source, start: hit.start, end: hit.end, text: quick.value, raw: hit.raw,
      page: quick.page, box: quick.box, shown: hit.text,
    };
    setPending((p) => ({
      ...p,
      texts: [
        ...p.texts.filter((t) => !(t.line === hit.line && t.start < hit.end && hit.start < t.end)),
        ...(quick.value === hit.text ? [] : [entry]),
      ],
    }));
    setQuick(null);
  }

  async function applyAll() {
    if (!id || !pendingCount) return;
    setEditError(null);
    setEditing(true);
    applyingRef.current = true;
    setProgress({ gen_id: id, status: "running", percent: 0, stage: "Submitting your changes…", error: "" });
    try {
      await applyChanges(
        id,
        pending.texts.map(({ line, source, start, end, text, raw }) => ({ line, source, start, end, text, raw })),
        pending.pages.map(({ index, instruction, figures }) => ({ index, instruction, figures: figures.map((f) => f.file) })),
        pending.deck ? { instruction: pending.deck.instruction, figures: pending.deck.figures.map((f) => f.file) } : null,
      );
      setWatch(true);
    } catch (err) {
      applyingRef.current = false;
      setEditing(false);
      setProgress(null);
      setEditError(errMsg(err));
    }
  }

  // Applied: the queue is done with. Failed: it stays, to retry or trim.
  const finishApply = useCallback((ok: boolean) => {
    if (!applyingRef.current) return;
    applyingRef.current = false;
    if (ok) setPending(NOTHING);
  }, [setPending]);

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
        finishApply(event.status === "succeeded" && !(fresh?.error || "").trim());
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
          } else if (fresh.job_running) {
            return; // edit still running
          } else if (!(fresh.error || "").trim()) {
            await loadFrames();
            setPdfBust(Date.now());
            setEditText("");
            finishApply(true);
          } else {
            finishApply(false);
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
  }, [id, metaStatus, editing, progressRunning, loadMeta, loadFrames]);

  // The frames API returns a synctex-resolved PDF `page` per frame (index and
  // page diverge with section splash pages, \pause, allowframebreaks…).
  // `page` maps the viewer page to a frame; `index` is the key for the edit API.
  // A notesheet can run over several pages: it covers first_page..page.
  const frame = frames.find((f) => (f.first_page ?? f.page) <= page && page <= f.page);

  function attach(files: Iterable<File>) {
    const images = [...files].filter((f) => f.type.startsWith("image/") || /\.(heic|heif)$/i.test(f.name));
    if (!images.length) return;
    setEditError(null);
    setAttached((a) => {
      const room = MAX_ATTACHED - a.length;
      if (images.length > room) setEditError(`At most ${MAX_ATTACHED} images per request`);
      return [...a, ...images.slice(0, Math.max(0, room)).map((file) => ({ file, url: URL.createObjectURL(file) }))];
    });
  }

  function unattach(index: number) {
    setAttached((a) => {
      URL.revokeObjectURL(a[index].url);
      return a.filter((_, i) => i !== index);
    });
  }

  // Queue a request for this slide or the whole deck; a second one for the same
  // target is added to the first. Attached images are uploaded now.
  async function handleQueue(e: FormEvent) {
    e.preventDefault();
    if (!id || !editText.trim()) return setEditError("Please describe the change");
    if (scope === "page" && !frame) return setEditError("This page has no editable slide");
    setEditError(null);
    let added: DeckFigure[] = [];
    if (attached.length) {
      setUploading(true);
      try {
        const files = await Promise.all(attached.map((a) => prepareFigure(a.file)));
        added = (await addFigures(id, files, files.map((f) => f.name))).figures;
      } catch (err) {
        setEditError(errMsg(err));
        return;
      } finally {
        setUploading(false);
      }
      attached.forEach((a) => URL.revokeObjectURL(a.url));
      setAttached([]);
    }
    const text = editText.trim();
    setPending((p) => {
      if (scope === "deck") {
        const old = p.deck;
        return {
          ...p,
          deck: {
            instruction: old ? `${old.instruction}\n${text}` : text,
            figures: [...(old?.figures ?? []), ...added],
            ...(old?.shown ? { shown: `${old.shown}\n${text}` } : {}),
          },
        };
      }
      const target = frame!;
      const old = p.pages.find((r) => r.index === target.index);
      const entry: PendingPage = {
        index: target.index, page, title: target.title,
        instruction: old ? `${old.instruction}\n${text}` : text,
        figures: [...(old?.figures ?? []), ...added],
        ...(old?.shown ? { shown: `${old.shown}\n${text}` } : {}),
      };
      return { ...p, pages: [...p.pages.filter((r) => r.index !== target.index), entry] };
    });
    setEditText("");
  }

  // From the notebook chat: a request for one sheet (or all the notes), added
  // to the same queue as requests typed under Edit.
  function queueNote(notesPage: number | null, instruction: string, shown: string): string {
    const join = (a: string | undefined, b: string) => (a ? `${a}\n${b}` : b);
    const target = notesPage === null
      ? undefined
      : frames.find((f) => (f.first_page ?? f.page) <= notesPage && notesPage <= f.page);
    setPending((p) => {
      if (!target) {
        const old = p.deck;
        return { ...p, deck: {
          instruction: join(old?.instruction, instruction), figures: old?.figures ?? [],
          shown: join(old ? old.shown ?? old.instruction : undefined, shown),
        } };
      }
      const old = p.pages.find((r) => r.index === target.index);
      const entry: PendingPage = {
        index: target.index, page: notesPage ?? target.page, title: target.title,
        instruction: join(old?.instruction, instruction),
        figures: old?.figures ?? [],
        shown: join(old ? old.shown ?? old.instruction : undefined, shown),
      };
      return { ...p, pages: [...p.pages.filter((r) => r.index !== target.index), entry] };
    });
    return `Queued for ${target ? `"${target.title || "this sheet"}"` : "all the notes"}: press Apply above to work it in`;
  }

  function figureStrip(list: DeckFigure[]) {
    if (!list.length) return null;
    return (
      <span className="mt-1 flex flex-wrap gap-1">
        {list.map((f) => (
          <img key={f.file} src={figureUrl(id, f.file)} alt={f.name} title={f.name}
            className="h-10 w-10 border border-amber-200 object-cover" />
        ))}
      </span>
    );
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
  const notes = meta.kind === "notes";
  const home = notes ? "/learn/notes" : "/present";

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

  const textMarks = pending.texts.map((t): PageMark => ({ page: t.page, box: t.box, label: `${t.shown} → ${t.text}` }));
  const editPanel = (
    <div className="flex flex-col gap-4 p-4">
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
    {frame && frame.index === 0 && (
      <p className="border-t border-[var(--color-border)] pt-4 text-xs leading-relaxed text-slate-500">
        The title page is made from the deck's title, subtitle, author, institute and date. A
        request here changes those.
      </p>
    )}
    {frame && frame.index !== 0 && !notes && (
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
    <form onSubmit={(e) => void handleQueue(e)}
      className="space-y-3 border-t border-[var(--color-border)] pt-4">
      <div className="flex items-center justify-between gap-2">
        <label className="text-sm text-slate-600" htmlFor="change-instruction">Change</label>
        <Segmented label="What to change" value={scope} onChange={setScope} disabled={busy} options={[
          { value: "page", label: notes ? "This sheet" : "This page" },
          { value: "deck", label: notes ? "All notes" : "Whole deck" },
        ]} />
      </div>
      <p className="text-xs text-slate-500">
        {scope === "page" && `To fix wording, click the text on the ${notes ? "page" : "slide"}. `}
        Requests for any {notes ? "sheet and for all the notes" : "page and for the whole deck"} wait
        in one list and go to the model in one call when applied, with one compile.
      </p>
      {scope === "page" && frames.length > 0 && !frame && (
        <p id="edit-no-frame-hint" className="text-xs text-slate-500">
          This page has no editable slide (it may be an auto-generated section or
          continuation page). Go to a content slide, or change the whole deck.
        </p>
      )}
      <textarea id="change-instruction" className="input-field min-h-28 text-sm"
        value={editText} onChange={(e) => setEditText(e.target.value)}
        onPaste={(e) => {
          const files = [...e.clipboardData.files];
          if (files.some((f) => f.type.startsWith("image/"))) {
            e.preventDefault();
            attach(files);
          }
        }}
        placeholder={notes
          ? scope === "page"
            ? "e.g. Split the causes into their own heading"
            : "e.g. Shorter: keep only definitions and numbers"
          : scope === "page"
            ? "e.g. Change the bar chart to a line chart"
            : "e.g. Tighten all bullet points to one line and unify tense"}
        disabled={busy || (scope === "page" && !frame)}
        aria-describedby={scope === "page" && frames.length > 0 && !frame ? "edit-no-frame-hint" : undefined} />
      <div className="flex flex-wrap items-center gap-2">
        {attached.map((a, i) => (
          <span key={a.url} className="relative">
            <img src={a.url} alt={a.file.name} title={a.file.name}
              className="h-12 w-12 border border-[var(--color-border)] object-cover" />
            <button type="button" aria-label={`Remove ${a.file.name}`} disabled={uploading}
              className="absolute -right-1.5 -top-1.5 h-4 w-4 rounded-full bg-slate-700 text-[10px] leading-4 text-white"
              onClick={() => unattach(i)}>×</button>
          </span>
        ))}
        <button type="button" className="btn-secondary text-xs" disabled={busy || uploading
          || attached.length >= MAX_ATTACHED || (scope === "page" && !frame)}
          onClick={() => fileInputRef.current?.click()}>
          <ImagePlus className="h-4 w-4" /> Attach image
        </button>
        <input ref={fileInputRef} type="file" accept="image/*,.heic,.heif" multiple hidden
          onChange={(e) => { attach(e.target.files ?? []); e.target.value = ""; }} />
      </div>
      {editError && (
        <div role="alert" className="border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{editError}</div>
      )}
      <button type="submit" className="btn-primary w-full"
        disabled={busy || uploading || (scope === "page" && !frame)}>
        {uploading ? "Uploading images…" : busy ? "Working…" : "Add to changes"}
      </button>
      <p className="text-xs text-slate-500">
        Images can also be pasted into the box. If the result does not compile, the{" "}
        {notes ? "notes are" : "deck is"} left as it was.
      </p>
    </form>
    </div>
  );

  return (
    <div className={notes && succeeded ? "flex flex-col gap-4 [&>*]:shrink-0" : "space-y-6"}
      style={notes && succeeded ? { height: "calc(100vh - 8.25rem)" } : undefined}>
      {notes ? (
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex min-w-0 flex-1 items-center gap-3">
            <Link to={home} className="shrink-0 text-sm text-[var(--color-primary)] no-underline hover:underline">
              ← Notebooks
            </Link>
            <h1 className="min-w-0 truncate text-xl" title={displayTitle(meta)}>{displayTitle(meta)}</h1>
            <StatusBadge status={busy ? "running" : meta.status} />
            <span className="hidden truncate text-xs text-slate-400 xl:inline"
              title={meta.source_names.join(", ")}>
              {(meta.pdf_pages || meta.frame_count) > 0 ? `${meta.pdf_pages || meta.frame_count} pages` : ""}
              {llmCalls > 0 ? ` · ${meta.llm_model || "LLM"} · ${fmtTokens(meta.llm_prompt_tokens)} in / ${fmtTokens(meta.llm_completion_tokens)} out` : ""}
            </span>
          </div>
          {succeeded ? (
            <div className="relative shrink-0">
              <button type="button" className="inline-flex items-center gap-1 px-2 py-1 text-sm text-slate-500 hover:text-slate-800"
                aria-haspopup="menu" aria-expanded={moreOpen} onClick={() => setMoreOpen((o) => !o)}>
                <Download className="h-4 w-4" /> Export <ChevronDown className="h-3.5 w-3.5" />
              </button>
              {moreOpen && <div className="fixed inset-0 z-30" onClick={() => setMoreOpen(false)} />}
              {moreOpen && (
                <div role="menu" className="absolute right-0 top-full z-40 w-48 border border-[var(--color-border)] bg-white py-1 text-sm shadow-lg"
                  onClick={() => setMoreOpen(false)}>
                  <a role="menuitem" className="flex items-center gap-2 px-3 py-2 text-slate-700 no-underline hover:bg-slate-50" href={pdfUrl(id)} download>
                    <Download className="h-4 w-4" /> Notes PDF
                  </a>
                  <a role="menuitem" className="flex items-center gap-2 px-3 py-2 text-slate-700 no-underline hover:bg-slate-50" href={texUrl(id)} download>
                    <FileText className="h-4 w-4" /> TeX
                  </a>
                  <a role="menuitem" className="flex items-center gap-2 px-3 py-2 text-slate-700 no-underline hover:bg-slate-50" href={bundleUrl(id)} download>
                    <Package className="h-4 w-4" /> Overleaf zip
                  </a>
                  <div className="my-1 border-t border-[var(--color-border)]" />
                  <button type="button" role="menuitem" className="flex w-full items-center gap-2 px-3 py-2 text-slate-700 hover:bg-slate-50 disabled:opacity-40"
                    disabled={busy || regenerating} onClick={() => void handleRegenerate()}>
                    <RotateCw className={`h-4 w-4 ${regenerating ? "animate-spin" : ""}`} />
                    {regenerating ? "Regenerating…" : "Regenerate notes"}
                  </button>
                </div>
              )}
            </div>
          ) : !(failed && modelError) && (
            <button type="button" className="btn-secondary py-1.5 text-sm"
              disabled={busy || meta.status === "running" || regenerating}
              onClick={() => void handleRegenerate()}>
              <RotateCw className={`h-4 w-4 ${regenerating ? "animate-spin" : ""}`} />
              {regenerating ? "Regenerating…" : "Regenerate"}
            </button>
          )}
        </div>
      ) : (
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
            {(meta.pdf_pages || meta.frame_count) > 0 ? ` · ${meta.pdf_pages || meta.frame_count} pages` : ""}
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
              {!notes && (
                <a className="btn-secondary text-sm" href={pptxUrl(id, myFonts ?? undefined)} download
                  title="Editable PowerPoint: text boxes over the PDF's own graphics">
                  <Presentation className="h-4 w-4" /> PPTX
                </a>
              )}
              <a className="btn-secondary text-sm" href={texUrl(id)} download>
                <FileText className="h-4 w-4" /> TeX
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

      )}

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

      {succeeded && pendingCount > 0 && (
        <section className="space-y-3 border border-amber-300 bg-amber-50 p-4" aria-label="Queued changes">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="text-sm text-amber-900">
              {pendingCount} {pendingCount === 1 ? "change" : "changes"} waiting. Nothing is compiled until you
              apply them; marked text on the {notes ? "pages" : "slides"} shows where.
            </p>
            <div className="flex gap-2">
              <button type="button" className="btn-secondary text-sm" disabled={busy}
                onClick={() => setPending(NOTHING)}>
                Discard all
              </button>
              <button type="button" className="btn-primary text-sm" disabled={busy} onClick={() => void applyAll()}>
                {busy ? "Working…" : `Apply ${pendingCount === 1 ? "it" : `all ${pendingCount}`}`}
              </button>
            </div>
          </div>
          <ul className={`space-y-1 text-sm ${notes ? "max-h-28 overflow-auto" : ""}`}>
            {[...pending.texts].sort((a, b) => a.page - b.page).map((t) => (
              <li key={`t${t.line}:${t.start}`} className="flex items-start gap-2">
                <span className="shrink-0 font-mono text-xs text-amber-800">p.{t.page}</span>
                <span className="min-w-0 flex-1 break-words text-slate-700">
                  {(() => {
                    const d = changedPart(t.shown, t.text);
                    return (
                      <>
                        {d.lead}
                        {d.removed && <del className="bg-red-100 text-red-700">{d.removed}</del>}
                        {d.added && <ins className="bg-emerald-100 text-emerald-800 no-underline">{d.added}</ins>}
                        {d.trail}
                      </>
                    );
                  })()}
                </span>
                <button type="button" className="shrink-0 px-1 text-slate-500 hover:text-red-600" disabled={busy}
                  aria-label="Remove this change"
                  onClick={() => setPending((p) => ({ ...p, texts: p.texts.filter((x) => x !== t) }))}>×</button>
              </li>
            ))}
            {[...pending.pages].sort((a, b) => a.page - b.page).map((r) => (
              <li key={`p${r.index}`} className="flex items-start gap-2">
                <span className="shrink-0 font-mono text-xs text-amber-800">p.{r.page}</span>
                <span className="min-w-0 flex-1 whitespace-pre-wrap break-words text-slate-700">
                  {r.title ? `${r.title}: ` : ""}{r.shown ?? r.instruction}
                  {figureStrip(r.figures)}
                </span>
                <button type="button" className="shrink-0 px-1 text-slate-500 hover:text-red-600" disabled={busy}
                  aria-label="Remove this request"
                  onClick={() => setPending((p) => ({ ...p, pages: p.pages.filter((x) => x !== r) }))}>×</button>
              </li>
            ))}
            {pending.deck && (
              <li className="flex items-start gap-2">
                <span className="shrink-0 font-mono text-xs text-amber-800">all</span>
                <span className="min-w-0 flex-1 whitespace-pre-wrap break-words text-slate-700">
                  {notes ? "All notes" : "Whole deck"}: {pending.deck.shown ?? pending.deck.instruction}
                  {figureStrip(pending.deck.figures)}
                </span>
                <button type="button" className="shrink-0 px-1 text-slate-500 hover:text-red-600" disabled={busy}
                  aria-label="Remove the whole-deck request"
                  onClick={() => setPending((p) => ({ ...p, deck: undefined }))}>×</button>
              </li>
            )}
          </ul>
        </section>
      )}

      {succeeded && !notes && (
        <div className="grid items-start gap-4 lg:grid-cols-[1fr_320px]">
          <section className="app-surface flex min-w-0 flex-col p-3">
            <PdfViewer file={pdfUrl(id, pdfBust)} onPageChange={setPage}
              onTextClick={busy ? undefined : handleTextClick}
              marks={textMarks}
              markedPages={new Set([...pending.texts.map((t) => t.page), ...pending.pages.map((r) => r.page)])} />
          </section>
          <aside className="app-surface">{editPanel}</aside>
        </div>
      )}

      {succeeded && notes && (
        <div className="relative left-1/2 min-h-[440px] w-[calc(100vw-2rem)] flex-1 -translate-x-1/2">
          <NotebookWorkspace id={id} pdfBust={pdfBust} sourceKind={meta.source_kind} sourceNames={meta.source_names}
            title={displayTitle(meta)} notesPages={meta.pdf_pages ?? 0}
            editPanel={editPanel}
            onNotesTextClick={busy ? undefined : handleTextClick}
            notesMarks={textMarks}
            onQueueNote={busy ? undefined : queueNote}
            onNotesPageChange={setPage} />
        </div>
      )}
      {quick && (
        <QuickEdit state={quick} onClose={closeQuick} onSave={saveQuick}
          onChange={(value) => setQuick((q) => q && { ...q, value, error: undefined })}
          surface={notes ? "page" : "slide"} />
      )}

      {!notes && <Link to={home} className="text-sm text-[var(--color-primary)] underline">← Back to home</Link>}
    </div>
  );
}
