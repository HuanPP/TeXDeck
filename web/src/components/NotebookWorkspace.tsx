import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { ChevronLeft, ChevronRight, MessageSquareText } from "lucide-react";
import {
  askQuestion, deleteThread, getSourceMap, listThreads, pdfUrl, sourcePdfUrl, sourceTextUrl, threadImageUrl,
  type ChatAnchor, type ChatThread, type SourceAnchor, type SourceMap,
} from "../api";
import NotebookChat from "./NotebookChat";
import PdfScroll, { type PageSelection, type PdfScrollHandle, type Underline } from "./PdfScroll";
import type { PageMark, TextClick } from "./PdfViewer";
import Segmented from "./Segmented";

// Learn's notebook: the source on the left, the notes in the middle, the side
// panel (AI, edits) on the right. Double-clicking either PDF jumps the other to
// the matching place, through the notes' \src marks.

const COLS_KEY = "texdeck.notebook.cols";
const MIN_COL = 0.18;

type Cols = { left: number; right: number; rightOpen: boolean };

function loadCols(): Cols {
  try {
    const c = JSON.parse(localStorage.getItem(COLS_KEY) ?? "null");
    if (c && typeof c.left === "number" && typeof c.right === "number") {
      return { left: c.left, right: c.right, rightOpen: c.rightOpen !== false };
    }
  } catch { /* fall through */ }
  return { left: 0.36, right: 0.27, rightOpen: true };
}

// Notes → source: the heading the double-click falls under.
export function anchorAt(anchors: SourceAnchor[], page: number, y: number): SourceAnchor | null {
  let hit: SourceAnchor | null = null;
  for (const a of anchors) {
    if (a.notes_page < page || (a.notes_page === page && a.y <= y + 0.01)) hit = a;
    else break;
  }
  return hit ?? anchors[0] ?? null;
}

// Source → notes: the headings that cite this page most narrowly (a sheet
// citing pp. 3-6 gives way to its heading citing p. 6), in reading order; when
// none cites it, those citing the nearest page before it.
export function anchorsFor(anchors: SourceAnchor[], doc: number, page: number): SourceAnchor[] {
  const mine = anchors.filter((a) => a.doc === doc);
  let found = mine.filter((a) => a.page <= page && page <= a.to);
  if (!found.length) {
    const before = mine.filter((a) => a.page <= page);
    const start = before.length ? Math.max(...before.map((a) => a.page)) : Math.min(...mine.map((a) => a.page));
    found = mine.filter((a) => a.page === start);
  }
  const narrowest = Math.min(...found.map((a) => a.to - a.page));
  return found.filter((a) => a.to - a.page === narrowest);
}

type Props = {
  id: string;
  pdfBust: number;
  sourceKind: string;
  sourceNames: string[];
  title: string;
  editPanel: ReactNode; // the right column's Edit tab
  onNotesTextClick?: (click: TextClick) => void; // click-to-edit, while the Edit tab is open
  notesMarks?: PageMark[];
  onNotesPageChange?: (page: number) => void;
  // Queue a change to the notes (the sheet on that notes page, or all the
  // notes when null); returns what was queued, for the status line.
  onQueueNote?: (notesPage: number | null, instruction: string) => string;
};

export default function NotebookWorkspace({
  id, pdfBust, sourceKind, sourceNames, title, editPanel, onNotesTextClick, notesMarks, onNotesPageChange, onQueueNote,
}: Props) {
  const [sideTab, setSideTab] = useState<"ai" | "edit">("ai");
  const [threads, setThreads] = useState<ChatThread[]>([]);
  const [quotes, setQuotes] = useState<ChatAnchor[]>([]);
  const [openThread, setOpenThread] = useState<string | null>(null);
  const [selection, setSelection] = useState<(ChatAnchor & { at: { x: number; y: number } }) | null>(null);
  const [chatError, setChatError] = useState<string | null>(null);
  const [focusKey, setFocusKey] = useState(0);
  const [map, setMap] = useState<SourceMap | null>(null);
  const [doc, setDoc] = useState(1);
  const [cols, setColsState] = useState<Cols>(loadCols);
  const [text, setText] = useState<string | null>(null);
  const [status, setStatus] = useState("");
  const rootRef = useRef<HTMLDivElement | null>(null);
  const sourceRef = useRef<PdfScrollHandle | null>(null);
  const notesRef = useRef<PdfScrollHandle | null>(null);
  const cycle = useRef<{ key: string; i: number }>({ key: "", i: 0 });
  const jump = useRef<{ page: number; y?: number } | null>(null);

  const setCols = useCallback((next: Cols) => {
    setColsState(next);
    localStorage.setItem(COLS_KEY, JSON.stringify(next));
  }, []);

  useEffect(() => {
    let cancelled = false;
    getSourceMap(id).then((m) => { if (!cancelled) setMap(m); }, () => undefined);
    return () => { cancelled = true; };
  }, [id, pdfBust]);

  useEffect(() => {
    if (sourceKind !== "text") return;
    fetch(sourceTextUrl(id)).then((r) => (r.ok ? r.text() : "")).then(setText, () => setText(""));
  }, [id, sourceKind]);

  useEffect(() => {
    if (!status) return;
    const t = window.setTimeout(() => setStatus(""), 3500);
    return () => window.clearTimeout(t);
  }, [status]);

  // Switching documents remounts the viewer; the jump waits for it.
  useEffect(() => {
    const j = jump.current;
    if (j && sourceRef.current) {
      jump.current = null;
      sourceRef.current.scrollTo(j.page, j.y);
    }
  }, [doc]);

  // Threads: loaded with the notes (a recompile moves quotes from the notes),
  // then polled while an answer is on its way.
  const loadThreads = useCallback(() => listThreads(id).then(setThreads, () => undefined), [id]);
  useEffect(() => { void loadThreads(); }, [loadThreads, pdfBust]);
  const answering = threads.some((t) => t.messages.some((m) => m.status === "pending"));
  useEffect(() => {
    if (!answering) return;
    const timer = window.setInterval(() => void loadThreads(), 2000);
    return () => window.clearInterval(timer);
  }, [answering, loadThreads]);

  function select(where: "source" | "notes") {
    return (s: PageSelection | null) =>
      setSelection(s && {
        where, doc: where === "source" ? doc : 0, page: s.page, text: s.text, rects: s.rects, at: s.at,
        ...(s.area ? { area: true } : {}),
      });
  }

  function quoteSelection() {
    if (!selection) return;
    const { at: _at, ...anchor } = selection;
    setQuotes((q) => [...q, anchor]);
    setSelection(null);
    window.getSelection()?.removeAllRanges();
    setSideTab("ai");
    if (!cols.rightOpen) setCols({ ...cols, rightOpen: true });
    setFocusKey((k) => k + 1);
  }

  async function ask(question: string, thread: string | null) {
    setChatError(null);
    try {
      const made = await askQuestion(id, question, thread, quotes);
      setQuotes([]);
      setThreads((all) => [...all.filter((t) => t.id !== made.id), made]);
      if (openThread && openThread !== made.id) setOpenThread(null);
    } catch (e) {
      setChatError(e instanceof Error ? e.message : String(e));
      throw e;
    }
  }

  function goTo(a: ChatAnchor) {
    const y = a.rects[0]?.y ?? 0;
    if (a.where === "notes") return notesRef.current?.scrollTo(a.page, y);
    if (a.doc !== doc) {
      jump.current = { page: a.page, y };
      setDoc(a.doc);
    } else {
      sourceRef.current?.scrollTo(a.page, y);
    }
  }

  // "Add to notes": a change request for the sheet the question is about --
  // the quoted sheet, or the sheet that cites the quoted source page.
  function addToNotes(thread: ChatThread, answerId: string) {
    if (!onQueueNote) return;
    const at = thread.messages.findIndex((m) => m.id === answerId);
    const answer = thread.messages[at];
    const question = [...thread.messages.slice(0, at)].reverse().find((m) => m.role === "user");
    if (!answer || !question) return;
    const fromNotes = thread.anchors.find((a) => a.where === "notes" && !a.gone);
    const fromSource = thread.anchors.find((a) => a.where === "source");
    const cited = fromSource ? anchorsFor(anchors, fromSource.doc, fromSource.page)[0] : undefined;
    const notesPage = fromNotes?.page ?? cited?.notes_page ?? null;
    const quoted = thread.anchors.filter((a) => a.text).map((a) => `"${a.text.slice(0, 400)}"`).join(" ");
    const lines = [
      "Work this question and answer into the notes. Add only what it contributes that the notes do not "
        + "already say, as bullets under the heading it fits best (a new \\notehead only when none fits), "
        + "in the notes' own style: one checkable fact per bullet, no Q&A format.",
      fromSource ? `Source page: ${fromSource.page}${fromSource.doc > 1 ? ` of document ${fromSource.doc}` : ""}.` : "",
      quoted ? `Quoted: ${quoted}` : "",
      `Q: ${question.content}`,
      `A: ${answer.content.slice(0, 4000)}`,
    ];
    setStatus(onQueueNote(notesPage, lines.filter(Boolean).join("\n")));
  }

  function openFromText(threadId: string) {
    setOpenThread(threadId);
    setSideTab("ai");
    if (!cols.rightOpen) setCols({ ...cols, rightOpen: true });
  }

  const underlines = (where: "source" | "notes"): Underline[] => threads.flatMap((t) => t.anchors
    .filter((a) => a.where === where && !a.gone && (where === "notes" || a.doc === doc))
    .map((a) => ({
      key: t.id, page: a.page, rects: a.rects, number: t.number, area: a.area,
      active: t.id === openThread, title: t.messages[0]?.content ?? "",
    })));

  const anchors = map?.anchors ?? [];
  const linked = anchors.length > 0;

  function notesToSource(page: number, y: number) {
    const a = anchorAt(anchors, page, y);
    if (!a) return;
    const range = a.to > a.page ? `pp. ${a.page}–${a.to}` : `p. ${a.page}`;
    setStatus(`${a.title || "This part"} → source ${multi ? `${a.doc}, ` : ""}${range}`);
    if (a.doc !== doc) {
      jump.current = { page: a.page };
      setDoc(a.doc);
    } else {
      sourceRef.current?.scrollTo(a.page);
    }
  }

  function sourceToNotes(page: number) {
    const found = anchorsFor(anchors, doc, page);
    if (!found.length) return;
    const key = `${doc}:${page}`;
    const i = cycle.current.key === key ? (cycle.current.i + 1) % found.length : 0;
    cycle.current = { key, i };
    const a = found[i];
    setStatus(found.length > 1
      ? `${a.title || "Notes"} (${i + 1} of ${found.length}; double-click again for the next)`
      : `${a.title || "Notes"}`);
    notesRef.current?.scrollTo(a.notes_page, a.y);
  }

  // Drag a splitter: fractions of the workspace width, each column kept usable.
  function drag(which: "left" | "right") {
    return (e: React.PointerEvent) => {
      e.preventDefault();
      const root = rootRef.current;
      if (!root) return;
      const rect = root.getBoundingClientRect();
      const move = (ev: PointerEvent) => {
        const f = (ev.clientX - rect.left) / rect.width;
        if (which === "left") {
          const right = cols.rightOpen ? cols.right : 0;
          setCols({ ...cols, left: Math.min(Math.max(f, MIN_COL), 1 - right - MIN_COL) });
        } else {
          setCols({ ...cols, right: Math.min(Math.max(1 - f, MIN_COL), 1 - cols.left - MIN_COL) });
        }
      };
      const up = () => {
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", up);
        document.body.style.userSelect = "";
      };
      document.body.style.userSelect = "none";
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
    };
  }

  const multi = (map?.sources.length ?? sourceNames.length) > 1;
  const sources = map?.sources ?? sourceNames.map((name, i) => ({ index: i + 1, name, pages: 0 }));
  const current = sources.find((s) => s.index === doc);
  const splitter = "w-1.5 shrink-0 cursor-col-resize bg-transparent hover:bg-[var(--color-primary)]/30";

  return (
    <div ref={rootRef} className="relative flex h-full min-h-0 w-full">
      <section className="flex min-w-0 shrink-0 flex-col border border-[var(--color-border)] bg-white"
        style={{ width: `${cols.left * 100}%` }} aria-label="Source">
        {multi && (
          <div className="flex shrink-0 gap-1 overflow-x-auto border-b border-[var(--color-border)] bg-slate-50 px-2 pt-1.5">
            {sources.map((s) => (
              <button key={s.index} type="button" title={s.name}
                className={`max-w-40 shrink-0 truncate px-2.5 py-1 text-xs ${s.index === doc
                  ? "border border-b-0 border-[var(--color-border)] bg-white text-[var(--color-heading)]"
                  : "text-slate-500 hover:text-slate-800"}`}
                onClick={() => setDoc(s.index)}>
                {s.index}. {s.name}
              </button>
            ))}
          </div>
        )}
        <div className="min-h-0 flex-1">
          {sourceKind === "text" ? (
            <div className="h-full overflow-auto p-5">
              <pre className="whitespace-pre-wrap break-words font-[inherit] text-sm leading-relaxed text-slate-700">
                {text ?? "Loading…"}
              </pre>
            </div>
          ) : (
            <PdfScroll key={doc} ref={sourceRef} file={sourcePdfUrl(id, doc)}
              label={current?.name ?? "Source"}
              hint={linked ? "Double-click a page to find its notes" : undefined}
              onDoubleClick={linked ? (page) => sourceToNotes(page) : undefined}
              onSelect={select("source")} underlines={underlines("source")} onUnderlineClick={openFromText}
              showArea={selection?.where === "source" && Boolean(selection.area)} />
          )}
        </div>
      </section>
      <div className={splitter} onPointerDown={drag("left")} role="separator" aria-label="Resize source column" />
      <section className="flex min-w-0 flex-1 flex-col border border-[var(--color-border)] bg-white" aria-label="Notes">
        <PdfScroll ref={notesRef} file={pdfUrl(id, pdfBust)} label={title}
          hint={linked
            ? "Double-click a part to see its source page"
            : sourceKind === "pdf" && map
              ? "These notes have no page links (made before they existed); regenerate to link them to the source"
              : undefined}
          onDoubleClick={linked ? notesToSource : undefined}
          onTextClick={sideTab === "edit" ? onNotesTextClick : undefined}
          marks={sideTab === "edit" ? notesMarks : undefined} onPageChange={onNotesPageChange}
          onSelect={select("notes")} underlines={underlines("notes")} onUnderlineClick={openFromText}
          showArea={selection?.where === "notes" && Boolean(selection.area)} />
      </section>
      {cols.rightOpen ? (
        <>
          <div className={splitter} onPointerDown={drag("right")} role="separator" aria-label="Resize side column" />
          <aside className="flex min-w-0 shrink-0 flex-col border border-[var(--color-border)] bg-white"
            style={{ width: `${cols.right * 100}%` }}>
            <div className="flex shrink-0 items-center gap-2 border-b border-[var(--color-border)] px-2 py-1">
              <div className="min-w-0 flex-1">
                <Segmented label="Side panel" value={sideTab} onChange={setSideTab} options={[
                  { value: "ai", label: "Ask AI" },
                  { value: "edit", label: "Edit" },
                ]} />
              </div>
              <button type="button" className="p-1 text-slate-400 hover:text-slate-700" aria-label="Collapse side panel"
                title="Collapse" onClick={() => setCols({ ...cols, rightOpen: false })}>
                <ChevronRight className="h-4 w-4" />
              </button>
            </div>
            <div className={`min-h-0 flex-1 ${sideTab === "edit" ? "overflow-auto" : ""}`}>
              {sideTab === "edit" ? editPanel : (
                <NotebookChat threads={threads} quotes={quotes} focusKey={focusKey} error={chatError}
                  imageUrl={(name) => threadImageUrl(id, name)}
                  onAddToNotes={onQueueNote ? addToNotes : undefined}
                  onRemoveQuote={(i) => setQuotes((q) => q.filter((_, j) => j !== i))}
                  onAsk={ask} openThread={openThread} onOpenThread={setOpenThread} onGoTo={goTo}
                  onDelete={(t) => {
                    if (openThread === t) setOpenThread(null);
                    setThreads((all) => all.filter((x) => x.id !== t));
                    void deleteThread(id, t).catch(() => loadThreads());
                  }} />
              )}
            </div>
          </aside>
        </>
      ) : (
        <button type="button" className="ml-1.5 flex w-8 shrink-0 flex-col items-center gap-2 border border-[var(--color-border)] bg-white pt-2 text-slate-500 hover:text-slate-800"
          aria-label="Open side panel" title="Open side panel" onClick={() => setCols({ ...cols, rightOpen: true })}>
          <ChevronLeft className="h-4 w-4" />
        </button>
      )}
      {selection && (
        <button type="button" aria-label="Ask AI about the selection" onMouseDown={(e) => e.preventDefault()} onClick={quoteSelection}
          className="fixed z-40 inline-flex items-center gap-1.5 rounded-full bg-indigo-600 px-3 py-1.5 font-sans text-xs text-white shadow-lg hover:bg-indigo-700"
          style={{ left: Math.min(selection.at.x + 6, window.innerWidth - 120), top: selection.at.y + 6 }}>
          <MessageSquareText className="h-3.5 w-3.5" /> Ask AI
        </button>
      )}
      {status && (
        <div role="status" className="pointer-events-none absolute bottom-4 left-1/2 z-30 max-w-[80%] -translate-x-1/2 truncate bg-slate-800/90 px-4 py-2 text-sm text-white shadow-lg">
          {status}
        </div>
      )}
    </div>
  );
}
