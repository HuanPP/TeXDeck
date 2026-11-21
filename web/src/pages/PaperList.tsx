import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { FilePlus2, FileText, Search, Trash2, Upload } from "lucide-react";
import { deleteGeneration, listGenerations } from "../api";
import { createPaper } from "../paperApi";
import type { GenerationMeta } from "../types";
import { useAsk } from "../components/AskDialog";

const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));
const date = (iso: string) => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
};

// Papering's front page: the reader's LaTeX projects, newest edit first.
export default function PaperList() {
  const navigate = useNavigate();
  const [items, setItems] = useState<GenerationMeta[] | null>(null);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<"recent" | "name">("recent");
  const [creating, setCreating] = useState(false);
  const [ask, askDialog] = useAsk();

  const load = () => listGenerations().then((gens) => setItems(gens.filter((g) => g.kind === "paper")), (e) => setError(errMsg(e)));
  useEffect(() => { void load(); }, []);

  const remove = async (g: GenerationMeta) => {
    const ok = await ask({ title: `Delete "${g.title}"?`, message: "The project, its files and its agent sessions will be deleted.",
      confirm: "Delete", danger: true });
    if (ok === null) return;
    try {
      await deleteGeneration(g.id);
      await load();
    } catch (e) { setError(errMsg(e)); }
  };

  const shown = (items ?? [])
    .filter((g) => g.title.toLowerCase().includes(query.trim().toLowerCase()))
    .sort((a, b) => sort === "name" ? a.title.localeCompare(b.title) : b.updated_at.localeCompare(a.updated_at));

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-3xl">Papering</h1>
          <p className="mt-1 text-sm text-slate-500">Write LaTeX with an AI agent beside you: it edits the files, you see the PDF.</p>
        </div>
        <button type="button" className="btn-primary flex items-center gap-2" onClick={() => setCreating(true)}>
          <FilePlus2 className="h-4 w-4" /> New project
        </button>
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <label className="flex flex-1 items-center gap-2 border border-[var(--color-border)] bg-white px-3 py-2">
          <Search className="h-4 w-4 text-slate-400" />
          <input type="search" placeholder="Search projects" aria-label="Search projects" value={query}
            onChange={(e) => setQuery(e.target.value)} className="w-full bg-transparent text-sm outline-none" />
        </label>
        <select aria-label="Sort projects" value={sort} onChange={(e) => setSort(e.target.value as typeof sort)}
          className="input-field w-auto text-sm">
          <option value="recent">Recently edited</option>
          <option value="name">Name</option>
        </select>
      </div>
      {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
      {items === null ? (
        <p className="text-sm text-slate-400">Loading…</p>
      ) : shown.length ? (
        <div className="border border-[var(--color-border)] bg-white">
          <div className="grid grid-cols-[1fr_9rem_2.5rem] border-b border-[var(--color-border)] px-4 py-2 text-xs uppercase tracking-wide text-slate-400">
            <span>Project</span><span>Last edited</span><span />
          </div>
          {shown.map((g) => (
            <div key={g.id} className="grid cursor-pointer grid-cols-[1fr_9rem_2.5rem] items-center border-b border-[var(--color-border)] px-4 py-3 last:border-b-0 hover:bg-slate-50"
              onClick={() => navigate(`/paper/${g.id}`)}>
              <span className="flex min-w-0 items-center gap-3">
                <FileText className="h-4 w-4 shrink-0 text-[var(--color-primary)]" />
                <span className="truncate text-[var(--color-heading)]">{g.title}</span>
              </span>
              <span className="text-sm text-slate-500">{date(g.updated_at)}</span>
              <button type="button" aria-label={`Delete ${g.title}`} title="Delete"
                className="justify-self-end p-1 text-slate-400 hover:text-red-600"
                onClick={(e) => { e.stopPropagation(); void remove(g); }}>
                <Trash2 className="h-4 w-4" />
              </button>
            </div>
          ))}
        </div>
      ) : (
        <div className="border border-dashed border-[var(--color-border)] bg-white px-6 py-16 text-center">
          <p className="text-lg text-[var(--color-heading)]">{query ? "No matching projects" : "A blank page awaits"}</p>
          {!query && <button type="button" className="btn-secondary mt-4" onClick={() => setCreating(true)}>Create a project</button>}
        </div>
      )}
      {askDialog}
      {creating && <NewProject onClose={() => setCreating(false)} onCreated={(id) => navigate(`/paper/${id}`)} />}
    </div>
  );
}

function NewProject({ onClose, onCreated }: { onClose: () => void; onCreated: (id: string) => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [title, setTitle] = useState("");
  const [language, setLanguage] = useState<"en" | "zh">("en");
  const [archive, setArchive] = useState<File | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => { dialog.current?.showModal(); }, []);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setPending(true);
    setError("");
    try {
      const { id } = await createPaper(title.trim(), language, archive);
      onCreated(id);
    } catch (err) {
      setError(errMsg(err));
      setPending(false);
    }
  };

  return (
    <dialog ref={dialog} aria-labelledby="new-paper-title" className="m-auto w-[min(32rem,92vw)] border border-[var(--color-border)] p-0 shadow-xl backdrop:bg-slate-900/30"
      onCancel={(e) => { e.preventDefault(); if (!pending) onClose(); }}>
      <form onSubmit={submit} className="space-y-4 p-6">
        <h2 id="new-paper-title" className="text-xl text-[var(--color-heading)]">New project</h2>
        <label className="block text-sm text-slate-600">
          Project name
          <input autoFocus required maxLength={120} value={title} onChange={(e) => setTitle(e.target.value)}
            className="input-field mt-1" />
        </label>
        <fieldset className="text-sm text-slate-600">
          <legend>Start from</legend>
          <div className="mt-1 flex flex-wrap gap-4">
            <label className="flex items-center gap-2">
              <input type="radio" name="start" checked={!archive && language === "en"} onChange={() => { setArchive(null); setLanguage("en"); }} />
              Blank (English)
            </label>
            <label className="flex items-center gap-2">
              <input type="radio" name="start" checked={!archive && language === "zh"} onChange={() => { setArchive(null); setLanguage("zh"); }} />
              Blank (Chinese, ctex)
            </label>
          </div>
          <label className="mt-3 flex cursor-pointer items-center gap-2 border border-dashed border-[var(--color-border)] px-3 py-2 hover:border-[var(--color-primary)]">
            <Upload className="h-4 w-4 text-slate-400" />
            <span className="truncate">{archive ? archive.name : "Or import a .zip (e.g. downloaded from Overleaf)"}</span>
            <input type="file" accept=".zip,application/zip" hidden onChange={(e) => setArchive(e.target.files?.[0] ?? null)} />
          </label>
        </fieldset>
        {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
        <div className="flex justify-end gap-3">
          <button type="button" className="btn-secondary" disabled={pending} onClick={onClose}>Cancel</button>
          <button type="submit" className="btn-primary" disabled={pending || !title.trim()}>{pending ? "Creating…" : "Create project"}</button>
        </div>
      </form>
    </dialog>
  );
}
