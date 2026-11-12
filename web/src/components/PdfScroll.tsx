import { useEffect, useImperativeHandle, useRef, useState, type Ref } from "react";
import { Document, Page } from "react-pdf";
import type { PDFDocumentProxy } from "pdfjs-dist";
import { lineAround, type PageMark, type TextClick } from "./PdfViewer";

// A continuous, scrolling PDF reader for the notebook columns: every page in
// one column, rendered only near the viewport, with a text layer to select from.

const MIN_ZOOM = 0.5;
const MAX_ZOOM = 3;
const ZOOM_STEP = 0.25;
const GAP = 12;
const RENDER_MARGIN = 1200; // px above and below the viewport that get rendered

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
};

export default function PdfScroll({ file, label, ref, onDoubleClick, onTextClick, marks = [], onPageChange, hint }: Props) {
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
        <div ref={scrollRef} onScroll={onScroll} className="h-full overflow-auto bg-slate-100">
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
