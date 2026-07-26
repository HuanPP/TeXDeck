import { useEffect, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { FileUp, Trash2 } from "lucide-react";
import {
  createGeneration,
  deleteGeneration,
  listGenerations,
  listTemplates,
} from "../api";
import StatusBadge from "../components/StatusBadge";
import type { GenerationMeta, TemplateItem } from "../types";

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
  const [sourceTab, setSourceTab] = useState<"pdf" | "text">("pdf");
  const [files, setFiles] = useState<File[]>([]);
  const [dragOver, setDragOver] = useState(false);
  const [sourceText, setSourceText] = useState("");

  useEffect(() => {
    void (async () => {
      try {
        const [tpls, gens] = await Promise.all([listTemplates(), listGenerations()]);
        setTemplates(tpls);
        setItems(gens);
        if (tpls[0]) setTemplateKey(tpls[0].key);
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
    if (!title.trim()) return setSubmitError("Please enter a title");
    if (!templateKey) return setSubmitError("Please select a template");
    if (sourceTab === "pdf" && files.length === 0) return setSubmitError("Please add at least one PDF");
    if (sourceTab === "text" && !sourceText.trim()) return setSubmitError("Please paste text content");

    setSubmitting(true);
    try {
      const result = await createGeneration({
        title: title.trim(),
        template_key: templateKey,
        instruction: instruction.trim(),
        author: author.trim(),
        institute: institute.trim(),
        files: sourceTab === "pdf" ? files : undefined,
        source_text: sourceTab === "text" ? sourceText : undefined,
      });
      navigate(`/g/${result.id}`);
    } catch (e) {
      setSubmitError(errMsg(e));
    } finally {
      setSubmitting(false);
    }
  }

  async function handleDelete(id: string, itemTitle: string) {
    if (!window.confirm(`Delete "${itemTitle}"? This cannot be undone.`)) return;
    try {
      await deleteGeneration(id);
      setItems((prev) => prev.filter((g) => g.id !== id));
    } catch (e) {
      alert(errMsg(e));
    }
  }

  return (
    <div className="space-y-10">
      <section className="app-surface p-6">
        <h1 className="mb-6 text-2xl">New generation</h1>
        <form onSubmit={handleSubmit} className="space-y-6">
          <div>
            <label className="mb-2 block text-sm text-slate-600">Select template</label>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 md:grid-cols-4">
              {templates.map((t) => (
                <button key={t.key} type="button" onClick={() => setTemplateKey(t.key)}
                  className={`border px-3 py-3 text-left text-sm ${
                    t.key === templateKey
                      ? "border-[var(--color-primary)] bg-sky-50"
                      : "border-[var(--color-border)] bg-white hover:border-slate-300"
                  }`}>{t.name}</button>
              ))}
            </div>
          </div>

          <div>
            <label className="mb-2 block text-sm text-slate-600" htmlFor="title">Title</label>
            <input id="title" className="input-field" value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="e.g. Diffusion Models for Protein Design" required />
          </div>

          <div>
            <label className="mb-2 block text-sm text-slate-600" htmlFor="author">
              Author (optional)
            </label>
            <input id="author" className="input-field" value={author}
              onChange={(e) => setAuthor(e.target.value)}
              placeholder="e.g. Jane Doe, John Smith" />
          </div>

          <div>
            <label className="mb-2 block text-sm text-slate-600" htmlFor="institute">
              Institute (optional)
            </label>
            <input id="institute" className="input-field" value={institute}
              onChange={(e) => setInstitute(e.target.value)}
              placeholder="e.g. Dept. of Physics, MIT" />
          </div>

          <div>
            <div className="mb-2 flex gap-2">
              <button type="button"
                className={sourceTab === "pdf" ? "btn-primary text-sm" : "btn-secondary text-sm"}
                onClick={() => setSourceTab("pdf")}>Upload PDF</button>
              <button type="button"
                className={sourceTab === "text" ? "btn-primary text-sm" : "btn-secondary text-sm"}
                onClick={() => setSourceTab("text")}>Paste text</button>
            </div>
            {sourceTab === "pdf" ? (
              <>
                <label
                  className={`flex cursor-pointer flex-col items-center border border-dashed px-4 py-8 text-sm text-slate-600 ${
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
                  <input type="file" multiple accept="application/pdf,.pdf" className="hidden"
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
              <textarea className="input-field min-h-40" value={sourceText}
                onChange={(e) => setSourceText(e.target.value)}
                placeholder="Paste research report text here…" />
            )}
          </div>

          <div>
            <label className="mb-2 block text-sm text-slate-600" htmlFor="instruction">
              Extra instructions (optional)
            </label>
            <textarea id="instruction" className="input-field min-h-24" value={instruction}
              onChange={(e) => setInstruction(e.target.value)}
              placeholder="e.g. Keep it under 10 slides, one slide per experiment, skip the related work" />
          </div>

          {submitError && (
            <div className="border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">{submitError}</div>
          )}
          <button type="submit" className="btn-primary" disabled={submitting}>
            {submitting ? "Submitting…" : "Generate"}
          </button>
        </form>
      </section>

      <section>
        <h2 className="mb-4 text-xl">History</h2>
        {loadError && (
          <div className="mb-4 border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">{loadError}</div>
        )}
        {items.length === 0 && !loadError ? (
          <p className="text-sm text-slate-500">No generations yet</p>
        ) : (
          <ul className="divide-y divide-[var(--color-border)] border border-[var(--color-border)] bg-white">
            {items.map((g) => (
              <li key={g.id} className="flex items-center gap-4 px-4 py-3 hover:bg-slate-50">
                <button type="button" className="min-w-0 flex-1 text-left"
                  onClick={() => navigate(`/g/${g.id}`)}>
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="truncate font-medium text-[var(--color-heading)]">{g.title}</span>
                    <StatusBadge status={g.status} />
                  </div>
                  <div className="mt-1 flex flex-wrap gap-x-4 text-xs text-slate-500">
                    <span>{g.template_name || g.template_key}</span>
                    <span>{fmt(g.created_at)}</span>
                    {g.frame_count > 0 && <span>{g.frame_count} pages</span>}
                  </div>
                </button>
                <button type="button" className="btn-secondary shrink-0 px-2 py-1 text-red-600"
                  title="Delete" onClick={() => void handleDelete(g.id, g.title)}>
                  <Trash2 className="h-4 w-4" />
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
