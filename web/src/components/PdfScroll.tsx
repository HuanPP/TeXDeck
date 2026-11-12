import { useEffect, useImperativeHandle, useRef, useState, type Ref } from "react";
import { Document, Page } from "react-pdf";
import { SquareDashed } from "lucide-react";
import type { PDFDocumentProxy } from "pdfjs-dist";
import { lineAround, type PageBox, type PageMark, type TextClick } from "./PdfViewer";

// A continuous, scrolling PDF reader for the notebook columns: every page in
// one column, rendered only near the viewport, with a text layer to select from.

const MIN_ZOOM = 0.5;
const MAX_ZOOM = 3;
const ZOOM_STEP = 0.25;
const GAP = 12;
const RENDER_MARGIN = 1200; // px above and below the viewport that get rendered

// Text selected on one page: its words and the line boxes it covers.
export type PageSelection = {
  page: number; text: string; rects: PageBox[]; at: { x: number; y: number };
  area?: boolean; // a box dragged over the page (no text layer needed), not a text selection
};
// A quote asked about: underlined, with its thread's number at the top left.
export type Underline = {
  key: string; page: number; rects: PageBox[]; number: number; active: boolean; title: string; area?: boolean;
};

// The selection's boxes on one page, one per line, as fractions of the page.
function lineBoxes(rects: DOMRect[], page: DOMRect): PageBox[] {
  const lines: { l: number; t: number; r: number; b: number }[] = [];
  for (const r of [...rects].sort((a, b) => a.top - b.top || a.left - b.left)) {
    const mid = (r.top + r.bottom) / 2;
    const line = lines.find((x) => mid > x.t && mid < x.b);
    if (line) {
      line.l = Math.min(line.l, r.left); line.r = Math.max(line.r, r.right);
      line.t = Math.min(line.t, r.top); line.b = Math.max(line.b, r.bottom);
    } else {
      lines.push({ l: r.left, t: r.top, r: r.right, b: r.bottom });
    }
  }
  return lines.map((x) => ({
    x: (x.l - page.left) / page.width, y: (x.t - page.top) / page.height,
    w: (x.r - x.l) / page.width, h: (x.b - x.t) / page.height,
  }));
}

export type PdfScrollHandle = {
  // Scroll so the point (page, y as a fraction of the page height) sits near the top.
  scrollTo: (page: number, y?: number, flash?: boolean) => void;
};

type Props = {
  file: string | null;
  label: string;
  ref?: Ref<PdfScrollHandle>;
  onDoubleClick?: (page: number, y: number) => void;
  // A single click on text (fires only when no double-click follows).
  onTextClick?: (click: TextClick) => void;
  marks?: PageMark[];
  onPageChange?: (page: number) => void;
  hint?: string;
  // Text selected (null when the selection goes away).
  onSelect?: (selection: PageSelection | null) => void;
  underlines?: Underline[];
  onUnderlineClick?: (key: string) => void;
  // The dragged box stays drawn while the parent still holds that selection.
  showArea?: boolean;
};

export default function PdfScroll({
  file, label, ref, onDoubleClick, onTextClick, marks = [], onPageChange, hint, onSelect, underlines = [], onUnderlineClick,
  showArea = false,
}: Props) {
  // Area mode: the next drag draws a box (scanned pages, figures, formulas).
  const [areaMode, setAreaMode] = useState(false);
  const [drag, setDrag] = useState<{ page: number; x0: number; y0: number; x1: number; y1: number } | null>(null);
  const [area, setArea] = useState<{ page: number; box: PageBox } | null>(null);
  const areaMade = useRef(false); // the mouseup after an area drag is not a text selection
  const [sizes, setSizes] = useState<{ w: number; h: number }[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [zoom, setZoom] = useState(1);
  const [width, setWidth] = useState(0);
  const [view, setView] = useState({ top: 0, bottom: 1200 });
  const [current, setCurrent] = useState(1);
  const [flash, setFlash] = useState<{ page: number; y: number; key: number } | null>(null);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const clickTimer = useRef(0);
  const pendingScroll = useRef<{ page: number; y: number; flash: boolean } | null>(null);

  useEffect(() => {
    setSizes([]);
    setLoadError(null);
  }, [file]);

  useEffect(() => {
    const el = rootRef.current;
    if (!el) return;
    const update = () => setWidth(el.clientWidth);
    update();
    const observer = new ResizeObserver(update);
    observer.observe(el);
    return () => observer.disconnect();
  }, [file, loadError]);

  useEffect(() => () => window.clearTimeout(clickTimer.current), []);

  const pageWidth = Math.max(200, Math.round(Math.max(200, width - 28) * zoom));
  const heights = sizes.map((s) => Math.round((pageWidth * s.h) / s.w));
  const tops: number[] = [];
  heights.reduce((acc, h, i) => {
    tops[i] = acc;
    return acc + h + GAP;
  }, GAP);

  function doScroll(page: number, y: number, withFlash: boolean) {
    const el = scrollRef.current;
    const i = page - 1;
    if (!el || i < 0 || i >= heights.length) return;
    const target = tops[i] + y * heights[i] - Math.min(80, el.clientHeight * 0.15);
    el.scrollTo({ top: Math.max(0, target), behavior: "smooth" });
    if (withFlash) setFlash({ page, y, key: Date.now() });
  }

  useImperativeHandle(ref, () => ({
    scrollTo(page, y = 0, withFlash = true) {
      if (!heights.length) {
        pendingScroll.current = { page, y, flash: withFlash };
        return;
      }
      doScroll(page, y, withFlash);
    },
  }));

  // A jump asked for before the document had loaded runs once it has.
  useEffect(() => {
    const p = pendingScroll.current;
    if (p && heights.length && width) {
      pendingScroll.current = null;
      doScroll(p.page, p.y, p.flash);
    }
  });

  useEffect(() => {
    if (!flash) return;
    const t = window.setTimeout(() => setFlash(null), 1600);
    return () => window.clearTimeout(t);
  }, [flash]);

  function onScroll() {
    const el = scrollRef.current;
    if (!el) return;
    setView({ top: el.scrollTop, bottom: el.scrollTop + el.clientHeight });
    const mid = el.scrollTop + el.clientHeight / 3;
    let page = 1;
    tops.forEach((t, i) => { if (t <= mid) page = i + 1; });
    if (page !== current) {
      setCurrent(page);
      onPageChange?.(page);
    }
  }

  useEffect(() => {
    const el = scrollRef.current;
    if (el) setView({ top: el.scrollTop, bottom: el.scrollTop + el.clientHeight });
  }, [sizes, width]);

  // After a drag (or a double-click that selects a word): report the selection
  // if it lies in this viewer, clipped to the page it starts on.
  function handleMouseUp() {
    if (!onSelect) return;
    if (areaMade.current) {
      areaMade.current = false;
      return;
    }
    window.setTimeout(() => {
      const sel = window.getSelection();
      if (!sel || sel.isCollapsed || !sel.rangeCount) return onSelect(null);
      const range = sel.getRangeAt(0);
      const node = range.startContainer;
      const start = node instanceof Element ? node : node.parentElement;
      const pageEl = start?.closest<HTMLElement>("[data-page]");
      if (!pageEl || !scrollRef.current?.contains(pageEl)) return onSelect(null);
      const text = sel.toString().replace(/\s+/g, " ").trim();
      if (!text) return onSelect(null);
      const box = pageEl.getBoundingClientRect();
      const rects = [...range.getClientRects()].filter((r) =>
        r.width > 1 && r.height > 1 && r.top < box.bottom && r.bottom > box.top && r.left < box.right && r.right > box.left);
      if (!rects.length) return onSelect(null);
      const last = rects.reduce((a, b) => (b.bottom > a.bottom || (b.bottom === a.bottom && b.right > a.right) ? b : a));
      onSelect({ page: Number(pageEl.dataset.page), text, rects: lineBoxes(rects, box), at: { x: last.right, y: last.bottom } });
    }, 0);
  }

  async function handleLoad(pdf: PDFDocumentProxy) {
    const out: { w: number; h: number }[] = [];
    for (let n = 1; n <= pdf.numPages; n++) {
      const vp = (await pdf.getPage(n)).getViewport({ scale: 1 });
      out.push({ w: vp.width, h: vp.height });
    }
    setSizes(out);
    setCurrent((c) => Math.min(c, out.length));
  }

  if (!file) {
    return <div className="flex h-full items-center justify-center text-sm text-slate-500">No PDF</div>;
  }
  if (loadError) {
    return (
      <div role="alert" className="flex h-full items-center justify-center p-6 text-sm text-red-600">
        Failed to load PDF: {loadError}
      </div>
    );
  }

  const total = sizes.length;
  return (
    <div ref={rootRef} className="flex h-full min-h-0 w-full flex-col">
      <div className="flex shrink-0 items-center gap-2 border-b border-[var(--color-border)] bg-white px-3 py-1.5 text-xs text-slate-600">
        <span className="min-w-0 flex-1 truncate font-medium text-[var(--color-heading)]" title={label}>{label}</span>
        {total > 0 && <span className="font-mono">{current} / {total}</span>}
        <span className="h-3 w-px bg-[var(--color-border)]" aria-hidden="true" />
        {onSelect && (
          <button type="button" aria-pressed={areaMode}
            title="Select an area: for scanned pages, figures and formulas"
            className={`inline-flex items-center gap-1 px-1.5 py-0.5 ${areaMode ? "bg-indigo-600 text-white" : "hover:text-slate-900"}`}
            onClick={() => setAreaMode((m) => !m)}>
            <SquareDashed className="h-3.5 w-3.5" /> Area
          </button>
        )}
        <button type="button" className="px-1.5 hover:text-slate-900 disabled:opacity-30" aria-label="Zoom out"
          disabled={zoom <= MIN_ZOOM}
          onClick={() => setZoom((z) => Math.max(MIN_ZOOM, +(z - ZOOM_STEP).toFixed(2)))}>−</button>
        <button type="button" className="font-mono underline decoration-dotted" title="Fit width"
          onClick={() => setZoom(1)}>{Math.round(zoom * 100)}%</button>
        <button type="button" className="px-1.5 hover:text-slate-900 disabled:opacity-30" aria-label="Zoom in"
          disabled={zoom >= MAX_ZOOM}
          onClick={() => setZoom((z) => Math.min(MAX_ZOOM, +(z + ZOOM_STEP).toFixed(2)))}>+</button>
      </div>
      {hint && <p className="shrink-0 bg-slate-50 px-3 py-1 text-[11px] text-slate-400">{hint}</p>}
      <Document file={file} onLoadSuccess={(pdf) => void handleLoad(pdf)} className="min-h-0 flex-1"
        onLoadError={(err) => setLoadError(err.message)}
        loading={<div className="p-12 text-center text-sm text-slate-500">Rendering PDF…</div>}>
        <div ref={scrollRef} onScroll={onScroll} onMouseUp={handleMouseUp}
          onMouseDown={() => { setArea(null); onSelect?.(null); }} className="h-full overflow-auto bg-slate-100">
          <div className="relative mx-auto" style={{ width: pageWidth, height: (tops[total - 1] ?? 0) + (heights[total - 1] ?? 0) + GAP }}>
            {sizes.map((_, i) => {
              const n = i + 1;
              const near = tops[i] + heights[i] > view.top - RENDER_MARGIN && tops[i] < view.bottom + RENDER_MARGIN;
              return (
                <div key={n} data-page={n}
                  className={`absolute left-0 border border-[var(--color-border)] bg-white shadow-sm${onTextClick ? " deck-editable" : ""}`}
                  style={{ top: tops[i], width: pageWidth, height: heights[i] }}
                  onClick={(e) => {
                    if (!onTextClick) return;
                    window.clearTimeout(clickTimer.current);
                    if (e.detail > 1) return;
                    const selection = window.getSelection();
                    if (selection && !selection.isCollapsed) return; // selecting, not editing
                    const span = (e.target as HTMLElement).closest<HTMLElement>(".textLayer span");
                    if (!span || !(span.textContent ?? "").trim()) return;
                    const box = e.currentTarget.getBoundingClientRect();
                    clickTimer.current = window.setTimeout(() => {
                      const line = lineAround(span);
                      onTextClick({
                        ...line, page: n,
                        box: {
                          x: (line.rect.left - box.left) / box.width, y: (line.rect.top - box.top) / box.height,
                          w: line.rect.width / box.width, h: line.rect.height / box.height,
                        },
                      });
                    }, 260);
                  }}
                  onDoubleClick={(e) => {
                    if (!onDoubleClick) return;
                    window.clearTimeout(clickTimer.current);
                    const box = e.currentTarget.getBoundingClientRect();
                    window.getSelection()?.removeAllRanges();
                    onDoubleClick(n, (e.clientY - box.top) / box.height);
                  }}>
                  {near && (
                    <Page pageNumber={n} width={pageWidth} renderTextLayer renderAnnotationLayer={false}
                      loading={<div style={{ width: pageWidth, height: heights[i] }} />} />
                  )}
                  {marks.filter((m) => m.page === n).map((m, k) => (
                    <div key={k} title={m.label} aria-hidden="true"
                      className="pointer-events-none absolute z-10 rounded-sm bg-amber-300/40 outline outline-2 outline-amber-500"
                      style={{ left: `${m.box.x * 100}%`, top: `${m.box.y * 100}%`, width: `${m.box.w * 100}%`, height: `${m.box.h * 100}%` }} />
                  ))}
                  {areaMode && (
                    <div className="absolute inset-0 z-30 cursor-crosshair touch-none"
                      onPointerDown={(e) => {
                        e.preventDefault();
                        e.currentTarget.setPointerCapture(e.pointerId);
                        const b = e.currentTarget.getBoundingClientRect();
                        const x = (e.clientX - b.left) / b.width;
                        const y = (e.clientY - b.top) / b.height;
                        setDrag({ page: n, x0: x, y0: y, x1: x, y1: y });
                      }}
                      onPointerMove={(e) => {
                        if (!drag || drag.page !== n) return;
                        const b = e.currentTarget.getBoundingClientRect();
                        setDrag({ ...drag, x1: Math.min(1, Math.max(0, (e.clientX - b.left) / b.width)),
                          y1: Math.min(1, Math.max(0, (e.clientY - b.top) / b.height)) });
                      }}
                      onPointerUp={(e) => {
                        if (!drag || drag.page !== n) return;
                        const box = {
                          x: Math.min(drag.x0, drag.x1), y: Math.min(drag.y0, drag.y1),
                          w: Math.abs(drag.x1 - drag.x0), h: Math.abs(drag.y1 - drag.y0),
                        };
                        setDrag(null);
                        if (box.w < 0.02 || box.h < 0.01) return;
                        setAreaMode(false);
                        areaMade.current = true;
                        window.setTimeout(() => { areaMade.current = false; }, 50);
                        setArea({ page: n, box });
                        onSelect?.({ page: n, text: "", rects: [box], at: { x: e.clientX, y: e.clientY }, area: true });
                      }} />
                  )}
                  {(drag?.page === n || (showArea && area?.page === n)) && (() => {
                    const b = drag?.page === n
                      ? { x: Math.min(drag.x0, drag.x1), y: Math.min(drag.y0, drag.y1), w: Math.abs(drag.x1 - drag.x0), h: Math.abs(drag.y1 - drag.y0) }
                      : area!.box;
                    return (
                      <div aria-hidden="true" className="pointer-events-none absolute z-30 border-2 border-dashed border-indigo-600 bg-indigo-400/10"
                        style={{ left: `${b.x * 100}%`, top: `${b.y * 100}%`, width: `${b.w * 100}%`, height: `${b.h * 100}%` }} />
                    );
                  })()}
                  {underlines.filter((u) => u.page === n).map((u) => (
                    <div key={u.key}>
                      {u.rects.map((r, k) => (
                        <div key={k} aria-hidden="true"
                          className={`pointer-events-none absolute z-10 ${u.active ? "bg-indigo-400/15" : ""}`}
                          style={{
                            left: `${r.x * 100}%`, top: `${r.y * 100}%`, width: `${r.w * 100}%`,
                            // a little below the glyphs: boxes found in the PDF end at their bottom edge
                            height: u.area ? `${r.h * 100}%` : `calc(${r.h * 100}% + 2px)`,
                            ...(u.area
                              ? { border: `1.5px dashed ${u.active ? "rgb(79 70 229)" : "rgb(129 140 248 / 0.8)"}` }
                              : { borderBottom: `2px solid ${u.active ? "rgb(79 70 229)" : "rgb(129 140 248 / 0.8)"}` }),
                          }} />
                      ))}
                      {u.number > 0 && u.rects[0] && (
                        <button type="button" title={u.title} aria-label={`Open question ${u.number}`}
                          className={`absolute z-20 flex h-4 min-w-4 -translate-x-full -translate-y-1/2 items-center justify-center rounded-full px-1 font-sans text-[10px] leading-none text-white shadow ${u.active ? "bg-indigo-700" : "bg-indigo-500 hover:bg-indigo-700"}`}
                          style={{ left: `${u.rects[0].x * 100}%`, top: `${u.rects[0].y * 100}%` }}
                          onMouseDown={(e) => e.stopPropagation()}
                          onClick={(e) => { e.stopPropagation(); onUnderlineClick?.(u.key); }}
                          onDoubleClick={(e) => e.stopPropagation()}>
                          {u.number}
                        </button>
                      )}
                    </div>
                  ))}
                  {flash?.page === n && (
                    <div key={flash.key} aria-hidden="true"
                      className="notebook-flash pointer-events-none absolute inset-x-0 z-10 border-l-4 border-[var(--color-primary)] bg-sky-200/40"
                      style={{ top: `${Math.max(0, flash.y * 100 - 1)}%`, height: "7%" }} />
                  )}
                </div>
              );
            })}
          </div>
        </div>
      </Document>
    </div>
  );
}
