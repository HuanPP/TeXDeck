import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { getSourceMap, pdfUrl, sourcePdfUrl, sourceTextUrl, type SourceAnchor, type SourceMap } from "../api";
import PdfScroll, { type PdfScrollHandle } from "./PdfScroll";
import type { PageMark, TextClick } from "./PdfViewer";

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
  side: ReactNode; // the right column's content
  sideTitle: ReactNode;
  onNotesTextClick?: (click: TextClick) => void;
  notesMarks?: PageMark[];
  onNotesPageChange?: (page: number) => void;
};

export default function NotebookWorkspace({
  id, pdfBust, sourceKind, sourceNames, title, side, sideTitle, onNotesTextClick, notesMarks, onNotesPageChange,
}: Props) {
  const [map, setMap] = useState<SourceMap | null>(null);
  const [doc, setDoc] = useState(1);
  const [cols, setColsState] = useState<Cols>(loadCols);
  const [text, setText] = useState<string | null>(null);
  const [status, setStatus] = useState("");
  const rootRef = useRef<HTMLDivElement | null>(null);
  const sourceRef = useRef<PdfScrollHandle | null>(null);
  const notesRef = useRef<PdfScrollHandle | null>(null);
  const cycle = useRef<{ key: string; i: number }>({ key: "", i: 0 });
  const jump = useRef<{ page: number } | null>(null);

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
      sourceRef.current.scrollTo(j.page);
    }
  }, [doc]);

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
              onDoubleClick={linked ? (page) => sourceToNotes(page) : undefined} />
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
          onTextClick={onNotesTextClick} marks={notesMarks} onPageChange={onNotesPageChange} />
      </section>
      {cols.rightOpen ? (
        <>
          <div className={splitter} onPointerDown={drag("right")} role="separator" aria-label="Resize side column" />
          <aside className="flex min-w-0 shrink-0 flex-col border border-[var(--color-border)] bg-white"
            style={{ width: `${cols.right * 100}%` }}>
            <div className="flex shrink-0 items-center gap-2 border-b border-[var(--color-border)] px-2 py-1">
              <div className="min-w-0 flex-1">{sideTitle}</div>
              <button type="button" className="p-1 text-slate-400 hover:text-slate-700" aria-label="Collapse side panel"
                title="Collapse" onClick={() => setCols({ ...cols, rightOpen: false })}>
                <ChevronRight className="h-4 w-4" />
              </button>
            </div>
            <div className="min-h-0 flex-1 overflow-auto">{side}</div>
          </aside>
        </>
      ) : (
        <button type="button" className="ml-1.5 flex w-8 shrink-0 flex-col items-center gap-2 border border-[var(--color-border)] bg-white pt-2 text-slate-500 hover:text-slate-800"
          aria-label="Open side panel" title="Open side panel" onClick={() => setCols({ ...cols, rightOpen: true })}>
          <ChevronLeft className="h-4 w-4" />
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
