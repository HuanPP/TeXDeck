import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { ChevronRight, FileUp, ImagePlus, Palette, Pencil, Plus, RotateCw, Trash2, X } from "lucide-react";
import {
  createGeneration,
  deleteGeneration,
  listGenerations,
  listTemplates,
  regenerateGeneration,
  templatePreviewUrl,
} from "../api";
import Segmented from "../components/Segmented";
import StatusBadge from "../components/StatusBadge";
import { prepareFigure } from "../imageTools";
import { modelReady, useModel } from "../model";
import { displayTitle } from "../title";
import type { GenerationMeta, SlideLanguage, TemplateItem } from "../types";

const MAX_IMAGES = 12; // the server's figures.MAX_FIGURES

type PickedImage = { id: number; file: File; url: string; note: string };

const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));
const fmt = (iso: string) => {
  try {
    return new Date(iso).toLocaleString("en-US", { hour12: false });
  } catch {
    return iso;
  }
};

export default function Home() {
  const navigate = useNavigate();
  const [search] = useSearchParams();
  const [templates, setTemplates] = useState<TemplateItem[]>([]);
  const [items, setItems] = useState<GenerationMeta[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [title, setTitle] = useState("");
  const [author, setAuthor] = useState("");
  const [institute, setInstitute] = useState("");
  const [templateKey, setTemplateKey] = useState("");
  const [instruction, setInstruction] = useState("");
  const [language, setLanguage] = useState<SlideLanguage>("source");
  const [moreOpen, setMoreOpen] = useState(false);
  const { status: model, setPanelOpen } = useModel();
  // Unknown until the status loads; do not block the button on that.
  const ready = model === null || modelReady(model);
  const [sourceTab, setSourceTab] = useState<"pdf" | "text">("pdf");
  const [files, setFiles] = useState<File[]>([]);
  const [dragOver, setDragOver] = useState(false);
  const [sourceText, setSourceText] = useState("");
  const [images, setImages] = useState<PickedImage[]>([]);
  const [imageError, setImageError] = useState<string | null>(null);
  const [imageDragOver, setImageDragOver] = useState(false);
  const imageIdRef = useRef(0);
  const imagesRef = useRef(images);
  imagesRef.current = images;
  // Preview thumbnails that returned non-200: fall back to a name tile, no retry.
  const [previewFailed, setPreviewFailed] = useState<Set<string>>(new Set());
  const [query, setQuery] = useState("");
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);
  const [regeneratingId, setRegeneratingId] = useState<string | null>(null);
  const confirmTimerRef = useRef(0);

  useEffect(() => {
    void (async () => {
      try {
        const [tpls, gens] = await Promise.all([listTemplates(), listGenerations()]);
        setTemplates(tpls);
        setItems(gens);
        // ?template= comes from the template editor's "Make a deck with it".
        const wanted = tpls.find((t) => t.key === search.get("template")) ?? tpls[0];
        if (wanted) setTemplateKey(wanted.key);
      } catch (e) {
        setLoadError(errMsg(e));
      }
    })();
  }, []);

  // Dropping a file outside the dropzone must not navigate the tab away
  // (which would silently nuke the whole form).
  useEffect(() => {
    const prevent = (e: DragEvent) => e.preventDefault();
    window.addEventListener("dragover", prevent);
    window.addEventListener("drop", prevent);
    return () => {
      window.removeEventListener("dragover", prevent);
      window.removeEventListener("drop", prevent);
    };
  }, []);

  // While any listed generation is still running, poll the list so statuses
  // update in place; the interval goes away as soon as none are running.
  const anyRunning = items.some((g) => g.status === "running");
  useEffect(() => {
    if (!anyRunning) return;
    const timer = window.setInterval(() => {
      listGenerations().then(setItems).catch(() => undefined);
    }, 5_000);
    return () => window.clearInterval(timer);
  }, [anyRunning]);

  useEffect(() => () => window.clearTimeout(confirmTimerRef.current), []);
  useEffect(() => () => imagesRef.current.forEach((img) => URL.revokeObjectURL(img.url)), []);

  async function addImages(list: FileList | File[] | null) {
    if (!list) return;
    setImageError(null);
    const picked = Array.from(list).filter((f) => f.type.startsWith("image/") || /\.(heic|heif)$/i.test(f.name));
    const room = MAX_IMAGES - imagesRef.current.length;
    if (picked.length > room) setImageError(`At most ${MAX_IMAGES} images`);
    const added: PickedImage[] = [];
    for (const f of picked.slice(0, Math.max(0, room))) {
      try {
        const file = await prepareFigure(f);
        added.push({ id: ++imageIdRef.current, file, url: URL.createObjectURL(file), note: "" });
      } catch (e) {
        setImageError(errMsg(e));
      }
    }
    if (added.length) setImages((prev) => [...prev, ...added]);
  }
  const removeImage = (id: number) =>
    setImages((prev) => {
      const gone = prev.find((img) => img.id === id);
      if (gone) URL.revokeObjectURL(gone.url);
      return prev.filter((img) => img.id !== id);
    });
  const setImageNote = (id: number, note: string) =>
    setImages((prev) => prev.map((img) => (img.id === id ? { ...img, note } : img)));

  const addFiles = (list: FileList | File[] | null) => {
    if (!list) return;
    const pdfs = Array.from(list).filter(
      (f) => f.type === "application/pdf" || /\.pdf$/i.test(f.name),
    );
    if (pdfs.length) setFiles((prev) => [...prev, ...pdfs]);
  };
  const removeFileAt = (i: number) => setFiles((prev) => prev.filter((_, j) => j !== i));
  const moveFile = (i: number, d: -1 | 1) =>
    setFiles((prev) => {
      const next = [...prev];
      [next[i], next[i + d]] = [next[i + d], next[i]];
      return next;
    });
  const fmtSize = (bytes: number) =>
    bytes >= 1e6 ? `${(bytes / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1e3))} KB`;

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setSubmitError(null);
    if (!ready) return setPanelOpen(true);
    if (!templateKey) return setSubmitError("Please select a template");
    if (sourceTab === "pdf" && files.length === 0) return setSubmitError("Please add at least one PDF");
    if (sourceTab === "text" && !sourceText.trim()) return setSubmitError("Please paste text content");

    setSubmitting(true);
    try {
      const result = await createGeneration({
        title: title.trim(),
        language,
        template_key: templateKey,
        instruction: instruction.trim(),
        author: author.trim(),
        institute: institute.trim(),
        files: sourceTab === "pdf" ? files : undefined,
        source_text: sourceTab === "text" ? sourceText : undefined,
        images: images.map((img) => ({ file: img.file, note: img.note.trim() })),
      });
      navigate(`/g/${result.id}`);
    } catch (e) {
      setSubmitError(errMsg(e));
    } finally {
      setSubmitting(false);
    }
  }

  function armDelete(id: string) {
    window.clearTimeout(confirmTimerRef.current);
    setConfirmDeleteId(id);
    confirmTimerRef.current = window.setTimeout(() => setConfirmDeleteId(null), 4_000);
  }

  async function handleDelete(id: string) {
    window.clearTimeout(confirmTimerRef.current);
    setConfirmDeleteId(null);
    try {
      await deleteGeneration(id);
      setItems((prev) => prev.filter((g) => g.id !== id));
    } catch (e) {
      setHistoryError(errMsg(e));
    }
  }

  async function handleRegenerate(id: string) {
    setHistoryError(null);
    setRegeneratingId(id);
    try {
      const result = await regenerateGeneration(id);
      navigate(`/g/${result.id}`);
    } catch (e) {
      setHistoryError(errMsg(e));
      setRegeneratingId(null);
    }
  }

  const q = query.trim().toLowerCase();
  const visibleItems = q
    ? items.filter((g) =>
        `${displayTitle(g)} ${g.template_name || g.template_key}`.toLowerCase().includes(q),
      )
    : items;

  return (
    <div className="space-y-10">
      <section className="app-surface p-6">
        <h1 className="mb-6 text-2xl">New generation</h1>
        {model && !ready && (
          <div className="mb-6 flex flex-wrap items-center justify-between gap-3 border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
            <span>Connect a model first: your ChatGPT or Claude plan, or an API key.</span>
            <button type="button" className="btn-primary px-3 py-1 text-sm"
              onClick={() => setPanelOpen(true)}>
              Connect a model
            </button>
          </div>
        )}
        <form onSubmit={handleSubmit} className="space-y-6">
          <div>
            <div className="mb-2 flex items-center justify-between gap-3">
              <span className="text-sm text-slate-600">Material</span>
              <Segmented label="Material" value={sourceTab} onChange={setSourceTab}
                options={[{ value: "pdf", label: "PDF" }, { value: "text", label: "Paste text" }]} />
            </div>
            {sourceTab === "pdf" ? (
              <>
                <label
                  className={`flex cursor-pointer flex-col items-center border border-dashed px-4 py-8 text-sm text-slate-600 focus-within:ring-2 focus-within:ring-[var(--color-primary)] ${
                    dragOver
                      ? "border-[var(--color-primary)] bg-sky-50"
                      : "border-[var(--color-border)] bg-slate-50"
                  }`}
                  onDragOver={(e) => {
                    e.preventDefault();
                    setDragOver(true);
                  }}
                  onDragLeave={() => setDragOver(false)}
                  onDrop={(e) => {
                    e.preventDefault();
                    setDragOver(false);
                    addFiles(e.dataTransfer.files);
                  }}
                >
                  <FileUp className="mb-2 h-5 w-5" />
                  {files.length ? "Add another PDF" : "Click to select or drop PDFs"}
                  <span className="mt-1 text-xs text-slate-400">Order below = reading order</span>
                  <input type="file" multiple accept="application/pdf,.pdf" className="sr-only"
                    onChange={(e) => {
                      addFiles(e.target.files);
                      e.target.value = "";
                    }} />
                </label>
                {files.length > 0 && (
                  <ul className="mt-2 space-y-1">
                    {files.map((f, i) => (
                      <li key={`${f.name}-${i}`}
                        className="flex items-center gap-2 border border-[var(--color-border)] bg-white px-2 py-1 text-sm">
                        <span className="text-xs text-slate-400">{i + 1}.</span>
                        <span className="min-w-0 flex-1 truncate">{f.name}</span>
                        <span className="shrink-0 text-xs text-slate-400">{fmtSize(f.size)}</span>
                        <button type="button" title="Move up" disabled={i === 0}
                          className="px-1 text-slate-500 hover:text-slate-700 disabled:opacity-30"
                          onClick={() => moveFile(i, -1)}>↑</button>
                        <button type="button" title="Move down" disabled={i === files.length - 1}
                          className="px-1 text-slate-500 hover:text-slate-700 disabled:opacity-30"
                          onClick={() => moveFile(i, 1)}>↓</button>
                        <button type="button" title="Remove"
                          className="px-1 text-red-600 hover:text-red-700"
                          onClick={() => removeFileAt(i)}>
                          <Trash2 className="h-3 w-3" />
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </>
            ) : (
              <textarea className="input-field min-h-40" value={sourceText} aria-label="Pasted text"
                onChange={(e) => setSourceText(e.target.value)}
                placeholder="Paste research report text here…" />
            )}
          </div>

          <div>
            <div className="mb-2 flex items-baseline justify-between gap-3">
              <span className="text-sm text-slate-600">Images to include (optional)</span>
              <span className="text-xs text-slate-400">The model picks the slide, size and position</span>
            </div>
            {images.length > 0 && (
              <ul className="mb-2 grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4">
                {images.map((img) => (
                  <li key={img.id} className="relative border border-[var(--color-border)] bg-white">
                    <div className="flex aspect-video items-center justify-center bg-slate-100">
                      <img src={img.url} alt={img.file.name} className="max-h-full max-w-full object-contain" />
                    </div>
                    <button type="button" title="Remove" aria-label={`Remove ${img.file.name}`}
                      className="absolute right-1 top-1 flex h-6 w-6 items-center justify-center bg-white/90 text-slate-600 hover:text-red-600"
                      onClick={() => removeImage(img.id)}>
                      <X className="h-3.5 w-3.5" />
                    </button>
                    <input className="w-full border-0 border-t border-[var(--color-border)] px-2 py-1.5 text-xs focus:outline-none focus:ring-2 focus:ring-inset focus:ring-[var(--color-primary)]"
                      value={img.note} maxLength={300} aria-label={`Note for ${img.file.name}`}
                      placeholder="Where it goes (optional)"
                      onChange={(e) => setImageNote(img.id, e.target.value)} />
                  </li>
                ))}
              </ul>
            )}
            {images.length < MAX_IMAGES && (
              <label
                className={`flex cursor-pointer items-center justify-center gap-2 border border-dashed px-4 py-3 text-sm text-slate-600 focus-within:ring-2 focus-within:ring-[var(--color-primary)] ${
                  imageDragOver
                    ? "border-[var(--color-primary)] bg-sky-50"
                    : "border-[var(--color-border)] bg-slate-50"
                }`}
                onDragOver={(e) => {
                  e.preventDefault();
                  setImageDragOver(true);
                }}
                onDragLeave={() => setImageDragOver(false)}
                onDrop={(e) => {
                  e.preventDefault();
                  setImageDragOver(false);
                  void addImages(e.dataTransfer.files);
                }}
              >
                <ImagePlus className="h-4 w-4" />
                {images.length ? "Add more images" : "Add photos, screenshots or logos to put on the slides"}
                <input type="file" multiple accept="image/*,.heic,.heif" className="sr-only"
                  onChange={(e) => {
                    void addImages(e.target.files);
                    e.target.value = "";
                  }} />
              </label>
            )}
            {imageError && <p role="alert" className="mt-2 text-sm text-red-600">{imageError}</p>}
          </div>

          <div>
            <label className="mb-2 block text-sm text-slate-600">Template</label>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4">
              {templates.map((t) => {
                const selected = t.key === templateKey;
                const editLink = t.custom
                  ? { to: `/templates/${encodeURIComponent(t.key)}`, label: `Edit ${t.name}`, icon: <Pencil size={13} /> }
                  : t.customizable
                    ? { to: `/templates/new?base=${encodeURIComponent(t.key)}`, label: `Customize ${t.name}`, icon: <Palette size={13} /> }
                    : null;
                return (
                  <div key={t.key} className="group relative">
                  <button type="button" aria-pressed={selected}
                    onClick={() => setTemplateKey(t.key)}
                    className={`w-full overflow-hidden border bg-white text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-primary)] focus-visible:ring-offset-2 ${
                      selected
                        ? "border-[var(--color-primary)] ring-2 ring-[var(--color-primary)]"
                        : "border-[var(--color-border)] hover:border-slate-300"
                    }`}>
                    <div className="aspect-video w-full bg-slate-100">
                      {previewFailed.has(t.key) ? (
                        <div className="flex h-full items-center justify-center px-2 text-center text-xs text-slate-500">
                          {t.name}
                        </div>
                      ) : (
                        <img src={templatePreviewUrl(t.key)} alt="" loading="lazy"
                          className="h-full w-full object-cover"
                          onError={() =>
                            setPreviewFailed((prev) => new Set(prev).add(t.key))
                          } />
                      )}
                    </div>
                    <div className={`truncate px-2 py-1.5 pr-8 text-sm ${selected ? "bg-sky-50" : ""}`}>
                      {t.name}
                    </div>
                  </button>
                  {editLink && (
                    <Link to={editLink.to} title={editLink.label} aria-label={editLink.label}
                      className={`absolute bottom-1.5 right-1.5 flex h-6 w-6 items-center justify-center text-slate-500 hover:bg-slate-100 hover:text-slate-800 focus:opacity-100 ${
                        t.custom ? "" : "opacity-0 group-hover:opacity-100"}`}>
                      {editLink.icon}
                    </Link>
                  )}
                  </div>
                );
              })}
              <Link to="/templates/new"
                className="flex min-h-28 flex-col items-center justify-center gap-1 border border-dashed border-slate-300 text-sm text-slate-500 no-underline hover:border-slate-400 hover:text-slate-700">
                <Plus size={18} />
                New template
              </Link>
            </div>
          </div>

          <div className="grid gap-6 md:grid-cols-[1fr_auto]">
            <div>
              <label className="mb-2 block text-sm text-slate-600" htmlFor="title">Title (optional)</label>
              <input id="title" className="input-field" value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder="Leave empty to use the document's own title" />
            </div>
            <div>
              <span className="mb-2 block text-sm text-slate-600">Slide language</span>
              <Segmented label="Slide language" value={language} onChange={setLanguage} options={[
                { value: "source", label: "Same as material" },
                { value: "en", label: "English" },
                { value: "zh", label: "中文" },
              ]} />
            </div>
          </div>

          <div>
            <button type="button" className="inline-flex items-center gap-1 text-sm text-[var(--color-primary)]"
              aria-expanded={moreOpen} onClick={() => setMoreOpen((o) => !o)}>
              <ChevronRight className={`h-4 w-4 transition-transform ${moreOpen ? "rotate-90" : ""}`} />
              More options
              {!moreOpen && (author || institute || instruction) ? (
                <span className="text-xs text-slate-500">(filled in)</span>
              ) : null}
            </button>
            {moreOpen && (
              <div className="mt-4 space-y-6">
                <div className="grid gap-6 md:grid-cols-2">
                  <div>
                    <label className="mb-2 block text-sm text-slate-600" htmlFor="author">Author</label>
                    <input id="author" className="input-field" value={author}
                      onChange={(e) => setAuthor(e.target.value)}
                      placeholder="e.g. Jane Doe, John Smith" />
                  </div>
                  <div>
                    <label className="mb-2 block text-sm text-slate-600" htmlFor="institute">Institute</label>
                    <input id="institute" className="input-field" value={institute}
                      onChange={(e) => setInstitute(e.target.value)}
                      placeholder="e.g. Dept. of Physics, MIT" />
                  </div>
                </div>
                <div>
                  <label className="mb-2 block text-sm text-slate-600" htmlFor="instruction">
                    Extra instructions
                  </label>
                  <textarea id="instruction" className="input-field min-h-24" value={instruction}
                    onChange={(e) => setInstruction(e.target.value)}
                    placeholder="e.g. Keep it under 10 slides, one slide per experiment, skip the related work" />
                </div>
              </div>
            )}
          </div>

          {submitError && (
            <div className="border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">{submitError}</div>
          )}
          <button type="submit" className="btn-primary" disabled={submitting || !ready}
            title={ready ? undefined : "Connect a model first"}>
            {submitting ? "Submitting…" : "Generate"}
          </button>
        </form>
      </section>

      <section>
        <h2 className="mb-4 text-xl">History</h2>
        {loadError && (
          <div className="mb-4 border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">{loadError}</div>
        )}
        {historyError && (
          <div role="alert" className="mb-4 border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
            {historyError}
          </div>
        )}
        {items.length > 0 && (
          <input type="search" className="input-field mb-3" aria-label="Search history"
            placeholder="Search by title or template…" value={query}
            onChange={(e) => setQuery(e.target.value)} />
        )}
        {items.length === 0 && !loadError ? (
          <p className="text-sm text-slate-500">No generations yet</p>
        ) : visibleItems.length === 0 ? (
          <p className="text-sm text-slate-500">No generations match “{query.trim()}”</p>
        ) : (
          <ul className="divide-y divide-[var(--color-border)] border border-[var(--color-border)] bg-white">
            {visibleItems.map((g) => (
              <li key={g.id} className="flex items-center gap-4 px-4 py-3 hover:bg-slate-50">
                <button type="button" className="min-w-0 flex-1 text-left"
                  onClick={() => navigate(`/g/${g.id}`)}>
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="truncate font-medium text-[var(--color-heading)]">{displayTitle(g)}</span>
                    <StatusBadge status={g.status} />
                  </div>
                  <div className="mt-1 flex flex-wrap gap-x-4 text-xs text-slate-500">
                    <span>{g.template_name || g.template_key}</span>
                    {g.source_names.length > 0 && (
                      <span className="max-w-56 truncate" title={g.source_names.join(", ")}>
                        {g.source_names[0]}
                        {g.source_names.length > 1 ? ` +${g.source_names.length - 1} more` : ""}
                      </span>
                    )}
                    <span>{fmt(g.created_at)}</span>
                    {(g.pdf_pages || g.frame_count) > 0 && <span>{g.pdf_pages || g.frame_count} pages</span>}
                  </div>
                </button>
                <button type="button" className="btn-secondary shrink-0 px-2 py-1"
                  title="Regenerate" aria-label={`Regenerate "${displayTitle(g)}"`}
                  disabled={g.status === "running" || regeneratingId !== null}
                  onClick={() => void handleRegenerate(g.id)}>
                  <RotateCw className={`h-4 w-4 ${regeneratingId === g.id ? "animate-spin" : ""}`} />
                </button>
                {confirmDeleteId === g.id ? (
                  <span className="flex shrink-0 items-center gap-1 text-xs text-red-600">
                    Confirm delete?
                    <button type="button" className="btn-secondary px-2 py-1 text-red-600"
                      aria-label="Confirm delete" onClick={() => void handleDelete(g.id)}>✓</button>
                    <button type="button" className="btn-secondary px-2 py-1"
                      aria-label="Cancel delete" onClick={() => setConfirmDeleteId(null)}>✕</button>
                  </span>
                ) : (
                  <button type="button" className="btn-secondary shrink-0 px-2 py-1 text-red-600"
                    title="Delete" aria-label={`Delete "${displayTitle(g)}"`}
                    onClick={() => armDelete(g.id)}>
                    <Trash2 className="h-4 w-4" />
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
