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
  const pageRefs = useRef<Map<number, HTMLDivElement>>(new Map());
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const ignoreScroll = useRef(0);
  const scrollRaf = useRef(0);
  const pageNumberRef = useRef(1);
  const restoreRef = useRef<number | null>(null);
  const renderedRef = useRef(0);
  pageNumberRef.current = pageNumber;

  useEffect(() => {
    // New file (initial load or post-edit recompile): remember the current page
    // and restore it once the new document renders, instead of jumping to page 1.
    restoreRef.current = pageNumberRef.current;
    renderedRef.current = 0;
    setNumPages(0);
    setLoadError(null);
    pageRefs.current.clear();
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

  useEffect(() => () => cancelAnimationFrame(scrollRaf.current), []);

  function scrollToPage(n: number, smooth: boolean) {
    const container = scrollRef.current;
    const el = pageRefs.current.get(n);
    if (!container || !el) return;
    const delta = el.getBoundingClientRect().top - container.getBoundingClientRect().top;
    container.scrollTo({
      top: container.scrollTop + delta - 16,
      behavior: smooth ? "smooth" : "auto",
    });
  }

  function goToPage(next: number) {
    const n = Math.max(1, numPages ? Math.min(next, numPages) : next);
    setPageNumber(n);
    onPageChange?.(n);
    ignoreScroll.current = Date.now() + 400;
    scrollToPage(n, true);
  }

  function handleScroll() {
    if (scrollRaf.current) return;
    scrollRaf.current = requestAnimationFrame(() => {
      scrollRaf.current = 0;
      if (Date.now() < ignoreScroll.current || !scrollRef.current || !numPages) return;
      restoreRef.current = null; // user scrolled: don't yank the view afterwards
      const top = scrollRef.current.getBoundingClientRect().top;
      let best = 1;
      let bestDist = Infinity;
      for (let i = 1; i <= numPages; i++) {
        const el = pageRefs.current.get(i);
        if (!el) continue;
        const dist = Math.abs(el.getBoundingClientRect().top - top - 24);
        if (dist < bestDist) {
          bestDist = dist;
          best = i;
        }
      }
      if (best !== pageNumber) {
        setPageNumber(best);
        onPageChange?.(best);
      }
    });
  }

  function handleDocumentLoad(pdf: { numPages: number }) {
    setNumPages(pdf.numPages);
    if (restoreRef.current === null) return;
    const target = Math.min(restoreRef.current, pdf.numPages);
    restoreRef.current = target;
    if (target !== pageNumberRef.current) {
      // Old page no longer exists (deck shrank): clamp and tell the sidebar.
      setPageNumber(target);
      onPageChange?.(target);
    }
  }

  function handlePageRender() {
    renderedRef.current += 1;
    if (restoreRef.current === null || !numPages || renderedRef.current < numPages) return;
    const target = Math.min(restoreRef.current, numPages);
    restoreRef.current = null;
    ignoreScroll.current = Date.now() + 600;
    scrollToPage(target, false);
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
      <div ref={scrollRef} className="min-h-0 flex-1 overflow-auto bg-slate-100 p-4" onScroll={handleScroll}>
        <Document file={file} onLoadSuccess={handleDocumentLoad}
          onLoadError={(err) => setLoadError(err.message)}
          loading={<div className="p-12 text-center text-slate-500">Rendering PDF…</div>}>
          <div className="mx-auto flex w-fit flex-col gap-4">
            {Array.from({ length: numPages }, (_, i) => i + 1).map((n) => (
              <div key={n} ref={(el) => { if (el) pageRefs.current.set(n, el); else pageRefs.current.delete(n); }}
                className="border border-[var(--color-border)] bg-white shadow-sm">
                <Page pageNumber={n} width={pageWidth} renderTextLayer renderAnnotationLayer
                  onRenderSuccess={handlePageRender} />
              </div>
            ))}
          </div>
        </Document>
      </div>
    </div>
  );
}
