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

type Props = {
  file: string | null;
  onPageChange?: (page: number) => void;
};

export default function PdfViewer({ file, onPageChange }: Props) {
  const [numPages, setNumPages] = useState(0);
  const [pageNumber, setPageNumber] = useState(1);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [zoom, setZoom] = useState(1);
  const [containerWidth, setContainerWidth] = useState(0);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const pageNumberRef = useRef(1);
  pageNumberRef.current = pageNumber;

  useEffect(() => {
    // New file (initial load or post-edit recompile): keep the current page
    // (clamped in handleDocumentLoad) instead of jumping back to page 1.
    setNumPages(0);
    setLoadError(null);
  }, [file]);

  useEffect(() => {
    const el = scrollRef.current;
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
    <div className="flex h-full min-h-0 w-full flex-col">
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
      <div ref={scrollRef} className="min-h-0 flex-1 overflow-auto bg-slate-100 p-4">
        <Document file={file} onLoadSuccess={handleDocumentLoad}
          onLoadError={(err) => setLoadError(err.message)}
          loading={<div className="p-12 text-center text-slate-500">Rendering PDF…</div>}>
          {numPages > 0 && (
            <div className="mx-auto w-fit border border-[var(--color-border)] bg-white shadow-sm">
              <Page pageNumber={Math.min(pageNumber, numPages)} width={pageWidth}
                renderTextLayer renderAnnotationLayer />
            </div>
          )}
        </Document>
      </div>
    </div>
  );
}
