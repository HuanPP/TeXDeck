import { useEffect, useRef, useState } from "react";
import { Document, Page, pdfjs } from "react-pdf";
// The worker must be the same pdfjs-dist build react-pdf runs, or every PDF
// fails with "API version does not match Worker version". package.json pins
// pdfjs-dist to exactly react-pdf's version (server/tests/test_web_deps.py checks).
import workerSrc from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import "react-pdf/dist/Page/AnnotationLayer.css";
import "react-pdf/dist/Page/TextLayer.css";

pdfjs.GlobalWorkerOptions.workerSrc = workerSrc;

const MIN_ZOOM = 0.5;
const MAX_ZOOM = 3;
const ZOOM_STEP = 0.25;
const MAX_FIT_WIDTH = 1100;
const MAX_PAGE_WIDTH = 2000;
const THUMB_WIDTH = 104;

export type TextClick = { text: string; before: string; after: string; page: number; rect: DOMRect };

type Props = {
  file: string | null;
  onPageChange?: (page: number) => void;
  // Called when the user clicks (not drag-selects) text on the page.
  onTextClick?: (click: TextClick) => void;
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
function lineAround(span: HTMLElement): { text: string; before: string; after: string; rect: DOMRect } {
  const layer = span.closest(".textLayer");
  const own = { s: span, r: span.getBoundingClientRect() };
  if (!layer) return { text: span.textContent ?? "", before: "", after: "", rect: own.r };
  const all = pieces(layer);
  const piece = all.find(({ s }) => s === span || s.contains(span)) ?? own;
  const line = lineOf(all, piece);
  const h = piece.r.height;
  return { ...line, before: neighbour(all, line.rect, h, -1), after: neighbour(all, line.rect, h, 1) };
}

export default function PdfViewer({ file, onPageChange, onTextClick }: Props) {
  const [numPages, setNumPages] = useState(0);
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
    // New file (initial load or post-edit recompile): keep the current page
    // (clamped in handleDocumentLoad) instead of jumping back to page 1.
    setNumPages(0);
    setLoadError(null);
  }, [file]);

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

  function handleDocumentLoad(pdf: { numPages: number }) {
    setNumPages(pdf.numPages);
    const target = Math.min(pageNumberRef.current, pdf.numPages);
    if (target !== pageNumberRef.current) {
      // Old page no longer exists (deck shrank): clamp and tell the sidebar.
      setPageNumber(target);
      onPageChange?.(target);
    }
  }

  if (!file) {
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
      <Document file={file} onLoadSuccess={handleDocumentLoad} className="flex min-h-0 flex-1 flex-col"
        onLoadError={(err) => setLoadError(err.message)}
        loading={<div className="p-12 text-center text-slate-500">Rendering PDF…</div>}>
        <div ref={scrollRef} className="min-h-0 flex-1 overflow-auto bg-slate-100 p-4">
          {numPages > 0 && (
            <div className={`mx-auto w-fit border border-[var(--color-border)] bg-white shadow-sm${onTextClick ? " deck-editable" : ""}`}
              onClick={(e) => {
                if (!onTextClick) return;
                const selection = window.getSelection();
                if (selection && !selection.isCollapsed) return; // selecting to copy, not editing
                const span = (e.target as HTMLElement).closest<HTMLElement>(".textLayer span");
                if (!span || !(span.textContent ?? "").trim()) return;
                onTextClick({ ...lineAround(span), page: Math.min(pageNumber, numPages) });
              }}>
              <Page pageNumber={Math.min(pageNumber, numPages)} width={pageWidth}
                renderTextLayer renderAnnotationLayer />
            </div>
          )}
        </div>
        {numPages > 1 && (
          <div ref={stripRef} className="relative mt-3 flex shrink-0 gap-2 overflow-x-auto pb-1"
            aria-label="Pages">
            {Array.from({ length: numPages }, (_, i) => i + 1).map((n) => (
              <button key={n} type="button" onClick={() => goToPage(n)}
                ref={(el) => { if (el) thumbRefs.current.set(n, el); else thumbRefs.current.delete(n); }}
                aria-label={`Page ${n}`} aria-current={n === pageNumber ? "page" : undefined}
                className={`shrink-0 border-2 bg-white p-0.5 ${n === pageNumber
                  ? "border-[var(--color-primary)]"
                  : "border-transparent opacity-80 hover:border-slate-300 hover:opacity-100"}`}>
                <Page pageNumber={n} width={THUMB_WIDTH} renderTextLayer={false} renderAnnotationLayer={false}
                  loading={<div className="bg-slate-100" style={{ width: THUMB_WIDTH, height: THUMB_WIDTH * 9 / 16 }} />} />
                <span className="block pt-0.5 text-center font-mono text-[10px] text-slate-500">{n}</span>
              </button>
            ))}
          </div>
        )}
      </Document>
    </div>
  );
}
