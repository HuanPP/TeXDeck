import { useEffect, useRef, useState } from "react";
import { Document, Page, pdfjs } from "react-pdf";
import workerSrc from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import "react-pdf/dist/Page/AnnotationLayer.css";
import "react-pdf/dist/Page/TextLayer.css";

pdfjs.GlobalWorkerOptions.workerSrc = workerSrc;

type Props = {
  file: string | null;
  pageWidth?: number;
  onPageChange?: (page: number) => void;
};

export default function PdfViewer({ file, pageWidth = 640, onPageChange }: Props) {
  const [numPages, setNumPages] = useState(0);
  const [pageNumber, setPageNumber] = useState(1);
  const [loadError, setLoadError] = useState<string | null>(null);
  const pageRefs = useRef<Map<number, HTMLDivElement>>(new Map());
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const ignoreScroll = useRef(0);

  useEffect(() => {
    setNumPages(0);
    setPageNumber(1);
    setLoadError(null);
    pageRefs.current.clear();
  }, [file]);

  function goToPage(next: number) {
    const n = Math.max(1, numPages ? Math.min(next, numPages) : next);
    setPageNumber(n);
    onPageChange?.(n);
    ignoreScroll.current = Date.now() + 400;
    pageRefs.current.get(n)?.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function handleScroll() {
    if (Date.now() < ignoreScroll.current || !scrollRef.current || !numPages) return;
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
  }

  if (!file) {
    return <div className="flex h-full items-center justify-center text-sm text-slate-500">No PDF yet</div>;
  }
  if (loadError) {
    return (
      <div className="flex h-full items-center justify-center p-6 text-sm text-red-600">
        Failed to load PDF: {loadError}
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 w-full flex-col">
      <div className="mb-3 flex shrink-0 items-center justify-center gap-3 border border-[var(--color-border)] bg-white px-3 py-2">
        <button type="button" className="btn-secondary px-2 py-1 text-xs" disabled={pageNumber <= 1}
          onClick={() => goToPage(pageNumber - 1)}>Prev</button>
        <span className="font-mono text-sm text-slate-600">
          Page {pageNumber}{numPages ? ` / ${numPages}` : ""}
        </span>
        <button type="button" className="btn-secondary px-2 py-1 text-xs"
          disabled={numPages > 0 && pageNumber >= numPages}
          onClick={() => goToPage(pageNumber + 1)}>Next</button>
      </div>
      <div ref={scrollRef} className="min-h-0 flex-1 overflow-auto bg-slate-100 p-4" onScroll={handleScroll}>
        <Document file={file} onLoadSuccess={(pdf) => setNumPages(pdf.numPages)}
          onLoadError={(err) => setLoadError(err.message)}
          loading={<div className="p-12 text-center text-slate-500">Rendering PDF…</div>}>
          <div className="mx-auto flex w-fit flex-col gap-4">
            {Array.from({ length: numPages }, (_, i) => i + 1).map((n) => (
              <div key={n} ref={(el) => { if (el) pageRefs.current.set(n, el); else pageRefs.current.delete(n); }}
                className="border border-[var(--color-border)] bg-white shadow-sm">
                <Page pageNumber={n} width={pageWidth} renderTextLayer renderAnnotationLayer />
              </div>
            ))}
          </div>
        </Document>
      </div>
    </div>
  );
}
