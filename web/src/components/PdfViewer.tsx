import { useEffect, useRef, useState } from "react";
import { Loader2 } from "lucide-react";
import { Document, Page, pdfjs } from "react-pdf";
// The worker must be the same pdfjs-dist build react-pdf runs, or every PDF
// fails with "API version does not match Worker version". package.json pins
// pdfjs-dist to exactly react-pdf's version (server/tests/test_web_deps.py checks).
import workerSrc from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import "react-pdf/dist/Page/AnnotationLayer.css";
import "react-pdf/dist/Page/TextLayer.css";

pdfjs.GlobalWorkerOptions.workerSrc = workerSrc;

// For every <Document>: CMaps for CID-keyed fonts (without them Chinese set in
// Fandol renders blank), standard fonts for unembedded ones. See vite.config.ts.
export const PDF_OPTIONS = { cMapUrl: "/pdfjs/cmaps/", cMapPacked: true, standardFontDataUrl: "/pdfjs/standard_fonts/" };

const MIN_ZOOM = 0.5;
const MAX_ZOOM = 3;
const ZOOM_STEP = 0.25;
const MAX_FIT_WIDTH = 1100;
const MAX_PAGE_WIDTH = 2000;
const THUMB_WIDTH = 104;

// A box on the page as fractions of its width and height (zoom-proof).
export type PageBox = { x: number; y: number; w: number; h: number };

export type TextClick = {
  text: string;
  before: string;
  after: string;
  page: number;
  rect: DOMRect;
  box: PageBox;
};

// A queued change drawn over the page, until it is applied.
export type PageMark = { page: number; box: PageBox; label: string };

type Props = {
  file: string | null;
  onPageChange?: (page: number) => void;
  // Called when the user clicks (not drag-selects) text on the page.
  onTextClick?: (click: TextClick) => void;
  marks?: PageMark[];
  // Pages with queued changes get a dot on their thumbnail.
  markedPages?: Set<number>;
  // A job is changing this PDF: shown over the page, which stays readable.
  busy?: string | null;
};

type Piece = { s: HTMLElement; r: DOMRect };

function pieces(layer: Element): Piece[] {
  return Array.from(layer.querySelectorAll<HTMLElement>("span"))
    .filter((s) => !s.querySelector("span") && (s.textContent ?? "").trim())
    .map((s) => ({ s, r: s.getBoundingClientRect() }));
}

// The visible line around a text-layer span. pdf.js splits a line into
// several spans (font changes, inline math); join the neighbours on the same
// baseline, but stop at wide gaps so the other column is not pulled in.
function lineOf(all: Piece[], piece: Piece): { text: string; rect: DOMRect } {
  const base = piece.r;
  const mid = (base.top + base.bottom) / 2;
  const row = all.filter(({ r }) => r.top <= mid && r.bottom >= mid).sort((a, b) => a.r.left - b.r.left);
  const at = row.findIndex(({ s }) => s === piece.s);
  const gap = base.height * 1.5;
  let lo = at;
  let hi = at;
  while (lo > 0 && row[lo].r.left - row[lo - 1].r.right < gap) lo--;
  while (hi < row.length - 1 && row[hi + 1].r.left - row[hi].r.right < gap) hi++;
  const part = row.slice(lo, hi + 1);
  const left = Math.min(...part.map(({ r }) => r.left));
  const right = Math.max(...part.map(({ r }) => r.right));
  const top = Math.min(...part.map(({ r }) => r.top));
  const bottom = Math.max(...part.map(({ r }) => r.bottom));
  return {
    text: part.map(({ s }) => s.textContent ?? "").join(""),
    rect: new DOMRect(left, top, right - left, bottom - top),
  };
}

// The line directly above or below in the same column: text of about the
// same size, overlapping horizontally, less than a line height away.
function neighbour(all: Piece[], line: DOMRect, height: number, dir: -1 | 1): string {
  const near = all.filter(({ r }) => {
    const gap = dir < 0 ? line.top - r.bottom : r.top - line.bottom;
    return gap > -height * 0.3 && gap < height && Math.abs(r.height - height) < height * 0.25
      && r.left < line.right && r.right > line.left;
  });
  if (!near.length) return "";
  near.sort((a, b) => (dir < 0 ? b.r.bottom - a.r.bottom : a.r.top - b.r.top));
  return lineOf(all, near[0]).text;
}

// The clicked line, plus the lines around it as context: a wrapped fragment
// or a repeated label is told apart by what surrounds it.
export function lineAround(span: HTMLElement): { text: string; before: string; after: string; rect: DOMRect } {
  const layer = span.closest(".textLayer");
  const own = { s: span, r: span.getBoundingClientRect() };
  if (!layer) return { text: span.textContent ?? "", before: "", after: "", rect: own.r };
  const all = pieces(layer);
  const piece = all.find(({ s }) => s === span || s.contains(span)) ?? own;
  const line = lineOf(all, piece);
  const h = piece.r.height;
  return { ...line, before: neighbour(all, line.rect, h, -1), after: neighbour(all, line.rect, h, 1) };
}

export default function PdfViewer({ file, onPageChange, onTextClick, marks = [], markedPages, busy }: Props) {
  // Double buffer: the PDF on screen (`shown`) stays put while a recompiled
  // `file` loads in a hidden layer, and is swapped out only once the new
  // current page has painted. An edit therefore never blanks the preview.
  const [shown, setShown] = useState(file);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [pageNumber, setPageNumber] = useState(1);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [zoom, setZoom] = useState(1);
  const [containerWidth, setContainerWidth] = useState(0);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const stripRef = useRef<HTMLDivElement | null>(null);
  const thumbRefs = useRef<Map<number, HTMLButtonElement>>(new Map());
  const pageNumberRef = useRef(1);
  pageNumberRef.current = pageNumber;

  useEffect(() => {
    // First file, or no file: show it directly. A later file loads hidden.
    if (!file || !shown) setShown(file);
    setLoadError(null);
  }, [file]);

  const incoming = file && shown && file !== shown ? file : null;
  const numPages = shown ? counts[shown] ?? 0 : 0;

  // Measure the always-mounted root: the scroll area lives inside <Document>,
  // which renders nothing until the PDF has loaded.
  useEffect(() => {
    const el = rootRef.current;
    if (!el) return;
    const update = () => setContainerWidth(el.clientWidth);
    update();
    const observer = new ResizeObserver(update);
    observer.observe(el);
    return () => observer.disconnect();
  }, [file, loadError]);

  function goToPage(next: number) {
    const n = Math.max(1, numPages ? Math.min(next, numPages) : next);
    if (n === pageNumberRef.current) return;
    setPageNumber(n);
    onPageChange?.(n);
    scrollRef.current?.scrollTo({ top: 0, left: 0 });
  }

  // Keep the current page's thumbnail in view without scrolling the window.
  useEffect(() => {
    const strip = stripRef.current;
    const thumb = thumbRefs.current.get(pageNumber);
    if (!strip || !thumb) return;
    const left = thumb.offsetLeft;
    const right = left + thumb.offsetWidth;
    if (left < strip.scrollLeft || right > strip.scrollLeft + strip.clientWidth) {
      strip.scrollTo({ left: left - (strip.clientWidth - thumb.offsetWidth) / 2, behavior: "smooth" });
    }
  }, [pageNumber, numPages]);

  // Left/Right (and PageUp/PageDown) flip pages unless the user is typing.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
      const t = e.target as HTMLElement | null;
      if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
      if (e.key === "ArrowLeft" || e.key === "PageUp") {
        e.preventDefault();
        goToPage(pageNumberRef.current - 1);
      } else if (e.key === "ArrowRight" || e.key === "PageDown") {
        e.preventDefault();
        goToPage(pageNumberRef.current + 1);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  // The old page may no longer exist (deck shrank): clamp and tell the sidebar.
  function clampTo(count: number) {
    const target = Math.min(pageNumberRef.current, count);
    if (count && target !== pageNumberRef.current) {
      setPageNumber(target);
      onPageChange?.(target);
    }
  }

  function handleDocumentLoad(url: string, pdf: { numPages: number }) {
    setCounts((c) => ({ ...c, [url]: pdf.numPages }));
    if (url === shown) clampTo(pdf.numPages);
  }

  function swapTo(url: string, error?: string) {
    if (url !== incoming) return;
    setShown(url);
    setCounts((c) => ({ [url]: c[url] ?? 0 }));
    clampTo(counts[url] ?? 0);
    if (error) setLoadError(error);
  }

  if (!file && !shown) {
    return <div className="flex h-full items-center justify-center text-sm text-slate-500">No PDF yet</div>;
  }
  if (loadError) {
    return (
      <div role="alert" className="flex h-full items-center justify-center p-6 text-sm text-red-600">
        Failed to load PDF: {loadError}
      </div>
    );
  }

  const fitWidth = containerWidth > 0
    ? Math.min(MAX_FIT_WIDTH, Math.max(240, containerWidth - 34))
    : 620;
  const pageWidth = Math.round(Math.min(fitWidth * zoom, MAX_PAGE_WIDTH));
  const status = busy || (incoming ? "Refreshing preview…" : null);

  function layer(url: string, hidden: boolean) {
    const count = counts[url] ?? 0;
    const current = Math.min(pageNumber, count);
    return (
      <div key={url} aria-hidden={hidden || undefined}
        className={hidden
          ? "pointer-events-none invisible absolute inset-x-0 top-0"
          : `flex min-h-0 flex-1 flex-col transition-opacity ${status ? "opacity-60" : ""}`}>
        <Document file={url} options={PDF_OPTIONS} className="flex min-h-0 flex-1 flex-col"
          onLoadSuccess={(pdf) => handleDocumentLoad(url, pdf)}
          onLoadError={(err) => (hidden ? swapTo(url, err.message) : setLoadError(err.message))}
          loading={hidden ? null : (
            // Hold a slide's height while the first PDF loads.
            <div className="flex items-center justify-center bg-slate-100 text-slate-500"
              style={{ minHeight: Math.round(pageWidth * 9 / 16) + 32 }}>Rendering PDF…</div>
          )}>
          <div ref={hidden ? undefined : scrollRef} className="min-h-0 flex-1 overflow-auto bg-slate-100 p-4">
            {count > 0 && (
              <div className={`relative mx-auto w-fit border border-[var(--color-border)] bg-white shadow-sm${onTextClick && !status ? " deck-editable" : ""}`}
                onClick={(e) => {
                  if (!onTextClick || hidden) return;
                  const selection = window.getSelection();
                  if (selection && !selection.isCollapsed) return; // selecting to copy, not editing
                  const span = (e.target as HTMLElement).closest<HTMLElement>(".textLayer span");
                  if (!span || !(span.textContent ?? "").trim()) return;
                  const line = lineAround(span);
                  const page = e.currentTarget.getBoundingClientRect();
                  const box = {
                    x: (line.rect.left - page.left) / page.width,
                    y: (line.rect.top - page.top) / page.height,
                    w: line.rect.width / page.width,
                    h: line.rect.height / page.height,
                  };
                  onTextClick({ ...line, box, page: current });
                }}>
                <Page pageNumber={current} width={pageWidth} renderTextLayer renderAnnotationLayer
                  onRenderSuccess={hidden ? () => swapTo(url) : undefined} />
                {!hidden && marks.filter((m) => m.page === current).map((m, i) => (
                  <div key={i} title={m.label} aria-hidden="true"
                    className="pointer-events-none absolute z-10 rounded-sm bg-amber-300/40 outline outline-2 outline-amber-500"
                    style={{
                      left: `${m.box.x * 100}%`, top: `${m.box.y * 100}%`,
                      width: `${m.box.w * 100}%`, height: `${m.box.h * 100}%`,
                    }} />
                ))}
              </div>
            )}
          </div>
          {count > 1 && (
            <div ref={hidden ? undefined : stripRef} className="relative mt-3 flex shrink-0 gap-2 overflow-x-auto pb-1"
              aria-label="Pages">
              {Array.from({ length: count }, (_, i) => i + 1).map((n) => (
                <button key={n} type="button" onClick={() => goToPage(n)} tabIndex={hidden ? -1 : undefined}
                  ref={hidden ? undefined : (el) => { if (el) thumbRefs.current.set(n, el); else thumbRefs.current.delete(n); }}
                  aria-label={`Page ${n}`} aria-current={n === pageNumber ? "page" : undefined}
                  className={`shrink-0 border-2 bg-white p-0.5 ${n === pageNumber
                    ? "border-[var(--color-primary)]"
                    : "border-transparent opacity-80 hover:border-slate-300 hover:opacity-100"}`}>
                  <Page pageNumber={n} width={THUMB_WIDTH} renderTextLayer={false} renderAnnotationLayer={false}
                    loading={<div className="bg-slate-100" style={{ width: THUMB_WIDTH, height: THUMB_WIDTH * 9 / 16 }} />} />
                  <span className="flex items-center justify-center gap-1 pt-0.5 font-mono text-[10px] text-slate-500">
                    {markedPages?.has(n) && (
                      <span className="inline-block h-1.5 w-1.5 rounded-full bg-amber-500" aria-label="has changes" />
                    )}
                    {n}
                  </span>
                </button>
              ))}
            </div>
          )}
        </Document>
      </div>
    );
  }

  return (
    <div ref={rootRef} className="flex h-full min-h-0 w-full flex-col">
      <div className="mb-3 flex shrink-0 flex-wrap items-center justify-center gap-3 border border-[var(--color-border)] bg-white px-3 py-2">
        <button type="button" className="btn-secondary px-2 py-1 text-xs" disabled={pageNumber <= 1}
          onClick={() => goToPage(pageNumber - 1)}>Prev</button>
        <span className="font-mono text-sm text-slate-600">
          Page {pageNumber}{numPages ? ` / ${numPages}` : ""}
        </span>
        <button type="button" className="btn-secondary px-2 py-1 text-xs"
          disabled={numPages > 0 && pageNumber >= numPages}
          onClick={() => goToPage(pageNumber + 1)}>Next</button>
        <span className="h-4 w-px bg-[var(--color-border)]" aria-hidden="true" />
        <button type="button" className="btn-secondary px-2 py-1 text-xs" aria-label="Zoom out"
          disabled={zoom <= MIN_ZOOM}
          onClick={() => setZoom((z) => Math.max(MIN_ZOOM, +(z - ZOOM_STEP).toFixed(2)))}>−</button>
        <button type="button" className="font-mono text-xs text-slate-600 underline decoration-dotted"
          aria-label="Reset zoom to fit width" title="Reset zoom to fit width"
          onClick={() => setZoom(1)}>{Math.round(zoom * 100)}%</button>
        <button type="button" className="btn-secondary px-2 py-1 text-xs" aria-label="Zoom in"
          disabled={zoom >= MAX_ZOOM}
          onClick={() => setZoom((z) => Math.min(MAX_ZOOM, +(z + ZOOM_STEP).toFixed(2)))}>+</button>
      </div>
      <div className="relative flex min-h-0 flex-1 flex-col">
        {shown && layer(shown, false)}
        {incoming && layer(incoming, true)}
        {status && (
          <div role="status" aria-live="polite"
            className="pointer-events-none absolute left-1/2 top-6 z-20 flex -translate-x-1/2 items-center gap-2 border border-[var(--color-border)] bg-white/95 px-3 py-1.5 text-sm text-slate-700 shadow-md">
            <Loader2 className="h-4 w-4 animate-spin text-[var(--color-primary)]" />
            {status}
          </div>
        )}
      </div>
    </div>
  );
}
