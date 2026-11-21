import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { basicSetup } from "codemirror";
import { StreamLanguage } from "@codemirror/language";
import { stex } from "@codemirror/legacy-modes/mode/stex";
import { openSearchPanel } from "@codemirror/search";
import { Annotation, EditorState, Transaction } from "@codemirror/state";
import { EditorView, keymap } from "@codemirror/view";
import {
  ArrowLeft, Bot, ChevronDown, MessageSquarePlus, Scissors, ChevronRight, Crosshair, Download, File as FileIcon, FilePlus, Folder, FolderPlus,
  FolderUp, Pencil, RefreshCw, Search, Star, Trash2, Upload,
} from "lucide-react";
import PdfScroll, { type PdfScrollHandle } from "../components/PdfScroll";
import PaperAgent from "../components/PaperAgent";
import { useAsk } from "../components/AskDialog";
import {
  ENGINES, compile, deleteFile, forwardSync, getBuild, getProject, inverseSync, newFile, pdfUrl, readSource,
  renameFile, saveSettings, saveSource, uploadFiles, watchUrl, zipUrl, assetUrl, quoteLabel,
  type Engine, type Quote, type PaperBuild, type PaperProject, type PaperSource,
} from "../paperApi";

const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));
const leaf = (path: string) => path.split("/").at(-1) || path;
const folderOf = (path: string) => (path.includes("/") ? path.slice(0, path.lastIndexOf("/") + 1) : "");
const EDITABLE = /\.(tex|bib|bbl|sty|cls|bst|ltx|md|txt|csv|tsv|json|xml|svg|cfg|def|clo|tikz|pgf|lua|r|py|m|dat|log|yaml|yml)$/i;
const IMAGE = /\.(png|jpe?g|webp|gif)$/i;
const AUTO_KEY = "texdeck.paper.autocompile";
const COLS_KEY = "texdeck.paper.cols";
type Cols = { files: number; agent: number; split: number }; // sidebar px per tab; editor share of the rest
const DEFAULT_COLS: Cols = { files: 240, agent: 400, split: 0.5 };
const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

function useWide(query = "(min-width: 1024px)") {
  const [wide, setWide] = useState(() => window.matchMedia(query).matches);
  useEffect(() => {
    const m = window.matchMedia(query);
    const on = () => setWide(m.matches);
    m.addEventListener("change", on);
    return () => m.removeEventListener("change", on);
  }, [query]);
  return wide;
}

// A vertical bar between two columns; dragging it reports the pointer's x.
function Splitter({ onDrag, label }: { onDrag: (x: number) => void; label: string }) {
  return (
    <div role="separator" aria-orientation="vertical" aria-label={label} title="Drag to resize"
      className="group relative z-10 -mx-1 w-2 cursor-col-resize touch-none select-none"
      onPointerDown={(e) => {
        e.preventDefault();
        const el = e.currentTarget;
        el.setPointerCapture(e.pointerId);
        const move = (ev: PointerEvent) => onDrag(ev.clientX);
        const up = () => { el.removeEventListener("pointermove", move); el.removeEventListener("pointerup", up); };
        el.addEventListener("pointermove", move);
        el.addEventListener("pointerup", up);
      }}>
      <div className="mx-auto h-full w-px bg-[var(--color-border)] transition-colors group-hover:w-0.5 group-hover:bg-[var(--color-primary)]" />
    </div>
  );
}

// --------------------------------------------------------------------------
// Editor
// --------------------------------------------------------------------------

const external = Annotation.define<boolean>();

function SourceEditor({ path, text, onChange, onSave, viewRef }: {
  path: string; text: string; onChange: (text: string) => void; onSave: () => void;
  viewRef: React.RefObject<EditorView | null>;
}) {
  const host = useRef<HTMLDivElement>(null);
  const latest = useRef({ text, onChange, onSave });
  latest.current = { text, onChange, onSave };
  useLayoutEffect(() => {
    const view = new EditorView({
      parent: host.current!,
      state: EditorState.create({
        doc: latest.current.text,
        extensions: [
          keymap.of([{ key: "Mod-s", preventDefault: true, run: () => { latest.current.onSave(); return true; } }]),
          basicSetup,
          EditorView.lineWrapping,
          EditorView.contentAttributes.of({ "aria-label": "Source editor", spellcheck: "false", autocapitalize: "none" }),
          EditorView.theme({
            "&": { height: "100%", fontSize: "13px" },
            ".cm-scroller": { fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace" },
          }),
          ...(/\.(tex|sty|cls|ltx|tikz|pgf|bib)$/i.test(path) ? [StreamLanguage.define(stex)] : []),
          EditorView.updateListener.of((u) => {
            if (u.docChanged && u.transactions.some((t) => !t.annotation(external))) {
              latest.current.onChange(u.state.doc.toString());
            }
          }),
        ],
      }),
    });
    viewRef.current = view;
    return () => { viewRef.current = null; view.destroy(); };
  }, [path, viewRef]);
  // Text from disk (an agent's edit, a reload) replaces the document without
  // touching undo history.
  useLayoutEffect(() => {
    const view = viewRef.current;
    if (!view || view.state.doc.toString() === text) return;
    view.dispatch({
      changes: { from: 0, to: view.state.doc.length, insert: text },
      annotations: [external.of(true), Transaction.addToHistory.of(false)],
    });
  }, [text, viewRef]);
  return <div ref={host} className="paper-editor h-full min-h-0 overflow-hidden" />;
}

type Open = PaperSource & { path: string; base: string }; // base: the text last saved or loaded

// The open file: loads it, saves it 550 ms after typing stops, and on a change
// on disk either takes the new text (no unsaved edits) or reports a conflict.
function useSource(id: string, path: string) {
  const [open, setOpen] = useState<Open | undefined>();
  const [conflict, setConflict] = useState<PaperSource | null>(null);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const state = useRef<Open | undefined>(undefined);
  const activePath = useRef(path);
  const pending = useRef<Promise<boolean> | null>(null);
  const seq = useRef(0);
  activePath.current = path;
  const update = useCallback((next?: Open) => { state.current = next; setOpen(next); }, []);
  const current = open?.path === path ? open : undefined;
  const dirty = !!current && current.text !== current.base;

  const reload = useCallback(async () => {
    if (!path || !EDITABLE.test(path)) { update(undefined); return; }
    const n = ++seq.current;
    const stale = () => n !== seq.current || activePath.current !== path || !!pending.current;
    try {
      const latest = await readSource(id, path);
      if (stale()) return;
      const prev = state.current;
      if (prev?.path === path && prev.text !== prev.base) {
        if (prev.revision !== latest.revision) setConflict(latest);
        return;
      }
      if (prev?.path !== path || prev.revision !== latest.revision) update({ ...latest, base: latest.text, path });
      setConflict(null);
      setError("");
    } catch (e) {
      if (!stale()) setError(errMsg(e));
    }
  }, [id, path, update]);

  useEffect(() => {
    setConflict(null);
    setError("");
    void reload();
    return () => { seq.current++; };
  }, [reload]);

  const save = useCallback(async (overwrite?: PaperSource): Promise<boolean> => {
    if (pending.current && !(await pending.current)) return false;
    const draft = state.current;
    if (!draft || draft.path !== path || draft.text === draft.base) return true;
    if (conflict && !overwrite) return false;
    setSaving(true);
    seq.current++;
    const request = (async () => {
      try {
        const r = await saveSource(id, path, draft.text, (overwrite ?? draft).revision);
        if (activePath.current === path && state.current?.path === path) {
          update({ ...state.current, revision: r.revision, base: draft.text });
          setConflict(null);
          setError("");
        }
        return true;
      } catch (e) {
        if (activePath.current === path) setError(errMsg(e));
        return false;
      }
    })();
    pending.current = request;
    const ok = await request;
    pending.current = null;
    setSaving(false);
    void reload();
    return ok;
  }, [id, path, conflict, reload, update]);

  const edit = (text: string) => { if (state.current?.path === path) update({ ...state.current, text }); };
  const takeDisk = () => {
    if (conflict) { update({ ...conflict, base: conflict.text, path }); setConflict(null); setError(""); }
  };
  useEffect(() => {
    if (!dirty || saving || conflict || error) return;
    const t = window.setTimeout(() => void save(), 550);
    return () => window.clearTimeout(t);
  }, [current?.text, dirty, saving, conflict, error, save]);

  return { current, dirty, saving, conflict, error, edit, reload, save, takeDisk };
}

// --------------------------------------------------------------------------
// File tree
// --------------------------------------------------------------------------

function FileTree({ project, selected, onOpen, onAction }: {
  project: PaperProject; selected: string; onOpen: (path: string) => void;
  onAction: (action: "rename" | "delete" | "main", path: string, directory: boolean) => void;
}) {
  const [collapsed, setCollapsed] = useState(new Set<string>());
  const visible = project.entries.filter((e) => {
    const parts = e.path.split("/");
    return !parts.slice(0, -1).some((_, i) => collapsed.has(parts.slice(0, i + 1).join("/")));
  });
  return (
    <div className="min-h-0 flex-1 overflow-auto py-1 text-[13px]" aria-label="Project files">
      {visible.map((e) => {
        const depth = e.path.split("/").length - 1;
        const isMain = e.path === project.settings.main;
        const active = selected === e.path;
        return (
          <div key={e.path} className={`group flex items-center ${active ? "bg-sky-50 text-[var(--color-heading)]" : "hover:bg-slate-50"}`}>
            <button type="button" title={e.path} aria-expanded={e.directory ? !collapsed.has(e.path) : undefined}
              className="flex min-w-0 flex-1 items-center gap-1.5 py-1 pr-1 text-left"
              style={{ paddingLeft: 10 + depth * 14 }}
              onClick={() => {
                if (!e.directory) return onOpen(e.path);
                setCollapsed((prev) => {
                  const next = new Set(prev);
                  if (next.has(e.path)) next.delete(e.path); else next.add(e.path);
                  return next;
                });
              }}>
              {e.directory
                ? (collapsed.has(e.path) ? <ChevronRight className="h-3.5 w-3.5 shrink-0 text-slate-400" /> : <ChevronDown className="h-3.5 w-3.5 shrink-0 text-slate-400" />)
                : <span className="w-3.5 shrink-0" />}
              {e.directory ? <Folder className="h-3.5 w-3.5 shrink-0 text-slate-400" /> : <FileIcon className="h-3.5 w-3.5 shrink-0 text-slate-400" />}
              <span className="truncate">{leaf(e.path)}</span>
              {isMain && <span className="ml-1 shrink-0 bg-slate-100 px-1 text-[10px] uppercase text-slate-500">main</span>}
            </button>
            <span className="hidden shrink-0 items-center gap-0.5 pr-1 group-hover:flex">
              {!e.directory && /\.tex$/i.test(e.path) && !isMain && (
                <button type="button" title="Use as main file" aria-label={`Use ${e.path} as main file`} className="p-0.5 text-slate-400 hover:text-amber-500"
                  onClick={() => onAction("main", e.path, false)}><Star className="h-3.5 w-3.5" /></button>
              )}
              <button type="button" title="Rename or move" aria-label={`Rename ${e.path}`} className="p-0.5 text-slate-400 hover:text-slate-700"
                onClick={() => onAction("rename", e.path, e.directory)}><Pencil className="h-3.5 w-3.5" /></button>
              <button type="button" title="Delete" aria-label={`Delete ${e.path}`} className="p-0.5 text-slate-400 hover:text-red-600"
                onClick={() => onAction("delete", e.path, e.directory)}><Trash2 className="h-3.5 w-3.5" /></button>
            </span>
          </div>
        );
      })}
    </div>
  );
}

// --------------------------------------------------------------------------
// Workspace
// --------------------------------------------------------------------------

const toolBtn = "inline-flex items-center gap-1 px-1.5 py-1 text-slate-500 hover:text-slate-900 disabled:opacity-30";

export default function PaperWorkspace() {
  const { id = "" } = useParams();
  const [project, setProject] = useState<PaperProject | null>(null);
  const [loadError, setLoadError] = useState("");
  const [path, setPath] = useState("");
  const [left, setLeft] = useState<"files" | "agent">("files");
  const wide = useWide();
  const [pane, setPane] = useState<"side" | "editor" | "pdf">("editor"); // narrow screens: one column at a time
  const [cols, setCols] = useState<Cols>(() => {
    try { return { ...DEFAULT_COLS, ...JSON.parse(localStorage.getItem(COLS_KEY) ?? "{}") }; } catch { return DEFAULT_COLS; }
  });
  const grid = useRef<HTMLDivElement>(null);
  const saveCols = (next: Cols) => { setCols(next); localStorage.setItem(COLS_KEY, JSON.stringify(next)); };
  const [notice, setNotice] = useState("");
  const [live, setLive] = useState(false);
  const [build, setBuild] = useState<PaperBuild | null>(null);
  const [compiling, setCompiling] = useState(false);
  const [showLog, setShowLog] = useState(false);
  const [auto, setAuto] = useState(() => localStorage.getItem(AUTO_KEY) !== "off");
  const [changes, setChanges] = useState(0); // bumps when files change on disk or are saved
  const [assetVersion, setAssetVersion] = useState(0);
  const [agent, setAgent] = useState<{ running: string; version: number }>({ running: "", version: 0 });
  const source = useSource(id, path);
  const [ask, askDialog] = useAsk();
  const view = useRef<EditorView | null>(null);
  const pdf = useRef<PdfScrollHandle>(null);
  const [jump, setJump] = useState<{ path: string; line: number } | null>(null);
  const compiled = useRef(-1);
  const busy = useRef(false);
  const chooser = useRef<HTMLInputElement>(null);
  const folderChooser = useRef<HTMLInputElement>(null);
  const reloadSource = useRef(source.reload);
  reloadSource.current = source.reload;

  const refresh = useCallback(async () => {
    try {
      const p = await getProject(id);
      setProject(p);
      setBuild((b) => (b && b.pdf === p.build.pdf && b.running === p.build.running ? b : { ...b, ...p.build }));
      setPath((cur) => cur || p.settings.main);
    } catch (e) { setLoadError(errMsg(e)); }
  }, [id]);

  useEffect(() => { void refresh(); }, [refresh]);

  // Live: the server pushes {files, build} whenever either changes, from this
  // page, another tab or the agent.
  useEffect(() => {
    let socket: WebSocket | null = null;
    let closed = false;
    let retry = 0;
    let lastFiles = "";
    let lastPdf: string | null = null;
    const connect = () => {
      socket = new WebSocket(watchUrl(id));
      socket.onopen = () => { setLive(true); retry = 0; };
      socket.onmessage = (ev) => {
        const msg = JSON.parse(ev.data) as { files: string; build: PaperBuild; agent: { running: string; version: number } };
        setAgent(msg.agent);
        if (msg.files !== lastFiles) {
          if (lastFiles) { void refresh(); void reloadSource.current(); setChanges((c) => c + 1); setAssetVersion((v) => v + 1); }
          lastFiles = msg.files;
        }
        setBuild((b) => ({ ...msg.build, log: b?.log }));
        if (lastPdf !== null && msg.build.pdf !== lastPdf || !msg.build.running && msg.build.error) {
          void getBuild(id).then(setBuild, () => undefined);
        }
        lastPdf = msg.build.pdf;
      };
      socket.onclose = () => {
        setLive(false);
        if (!closed) window.setTimeout(connect, Math.min(10000, 1000 * 2 ** retry++));
      };
    };
    connect();
    return () => { closed = true; socket?.close(); };
  }, [id, refresh]);

  const runCompile = useCallback(async () => {
    if (busy.current) return;
    if (!(await source.save())) return;
    busy.current = true;
    compiled.current = changes;
    setCompiling(true);
    setNotice("");
    try {
      const b = await compile(id);
      setBuild(b);
      if (!b.ok && b.error) setShowLog(false);
    } catch (e) { setNotice(errMsg(e)); }
    finally { busy.current = false; setCompiling(false); }
  }, [id, source, changes]);

  // Auto-compile once the files have settled: after a save, an upload, or the
  // agent's edits (once it has finished, not after every file it touches).
  useEffect(() => {
    if (!auto || !project || compiled.current === changes || source.dirty || source.saving || compiling || build?.running
      || agent.running) return;
    // Opened with an up-to-date PDF: nothing to do. (Stale: sources changed while
    // no page was open, e.g. an upload elsewhere; build them.)
    if (changes === 0 && build?.pdf && !project.build.stale) { compiled.current = 0; return; }
    const t = window.setTimeout(() => void runCompile(), 850);
    return () => window.clearTimeout(t);
  }, [auto, project, changes, source.dirty, source.saving, compiling, build?.running, build?.pdf, runCompile, agent.running]);

  // After a save, count it as a change so auto-compile picks it up.
  const wasSaving = useRef(false);
  useEffect(() => {
    if (wasSaving.current && !source.saving) setChanges((c) => c + 1);
    wasSaving.current = source.saving;
  }, [source.saving]);

  // Jump the editor to a line (from a double-click on the PDF).
  useEffect(() => {
    const editor = view.current;
    if (!jump || jump.path !== source.current?.path || !editor) return;
    const line = editor.state.doc.line(Math.min(jump.line, editor.state.doc.lines));
    editor.dispatch({ selection: { anchor: line.from }, effects: EditorView.scrollIntoView(line.from, { y: "center" }) });
    editor.focus();
    setJump(null);
  }, [jump, source.current?.path]);

  // What the agent is told the user is looking at; passages go in only when added.
  const agentContext = () => {
    const editor = view.current;
    if (!editor || !source.current) return { path };
    return { path, line: editor.state.doc.lineAt(editor.state.selection.main.head).number };
  };

  // Selecting text in the editor or the PDF offers "Add to chat" beside the
  // selection; the passage becomes a card above the agent's message box.
  const [quotes, setQuotes] = useState<Quote[]>([]);
  const [offer, setOffer] = useState<{ x: number; y: number; origin: Element; take: () => Promise<Quote> } | null>(null);
  const offerBtn = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const check = (e: Event) => {
      if (offerBtn.current?.contains(e.target as Node)) return;
      window.setTimeout(() => {
        const editor = view.current;
        if (editor?.hasFocus) {
          const sel = editor.state.selection.main;
          const at = sel.empty ? null : editor.coordsAtPos(sel.head);
          if (!at || !editor.state.sliceDoc(sel.from, sel.to).trim()) return setOffer(null);
          const doc = editor.state.doc;
          const end = doc.lineAt(sel.to);
          const quote: Quote = {
            source: "tex", path, text: editor.state.sliceDoc(sel.from, sel.to),
            from: doc.lineAt(sel.from).number, to: end.from === sel.to && sel.to > sel.from ? end.number - 1 : end.number,
          };
          return setOffer({ x: at.left, y: sel.head >= sel.anchor ? at.bottom : at.top - 30, origin: editor.scrollDOM,
            take: async () => quote });
        }
        const s = window.getSelection();
        const range = s && !s.isCollapsed && s.rangeCount ? s.getRangeAt(0) : null;
        const pageEl = range?.startContainer.parentElement?.closest<HTMLElement>("[data-page]");
        const text = s?.toString().trim() ?? "";
        if (!range || !pageEl || !text || !pageEl.closest('section[aria-label="PDF preview"]')) return setOffer(null);
        const rects = [...range.getClientRects()].filter((r) => r.width > 0);
        if (!rects.length) return setOffer(null);
        const first = rects[0], last = rects[rects.length - 1];
        const page = Number(pageEl.dataset.page);
        setOffer({
          x: last.right, y: last.bottom, origin: pageEl,
          take: async () => {
            const at = await sourceAt(page, pageEl, first.left, first.top + first.height / 2);
            return { source: "pdf", page, text, ...(at ? { path: at.path, line: at.line } : {}) };
          },
        });
      }, 0);
    };
    const scrolled = (e: Event) => setOffer((o) => (o && (e.target as Node).contains?.(o.origin) ? null : o));
    document.addEventListener("mouseup", check);
    document.addEventListener("keyup", check);
    document.addEventListener("scroll", scrolled, true);
    return () => {
      document.removeEventListener("mouseup", check);
      document.removeEventListener("keyup", check);
      document.removeEventListener("scroll", scrolled, true);
    };
  }, [id, path]);
  const pushQuote = (q: Quote) => {
    setQuotes((qs) => (q.source === "pdf" && q.image ? [...qs, q]
      : qs.some((x) => x.text === q.text && quoteLabel(x) === quoteLabel(q)) ? qs : [...qs, q]).slice(-12));
    setLeft("agent");
    if (!wide) setPane("side");
  };
  const addQuote = async () => {
    const o = offer;
    setOffer(null);
    if (!o) return;
    pushQuote(await o.take());
    window.getSelection()?.removeAllRanges();
  };
  // Where a point of a page comes from in the source, if SyncTeX knows.
  const sourceAt = async (page: number, pageEl: Element, x: number, y: number) => {
    const size = pdf.current?.pageSize(page);
    const box = pageEl.getBoundingClientRect();
    if (!size) return null;
    return inverseSync(id, page, ((x - box.left) / box.width) * size.w, ((y - box.top) / box.height) * size.h).catch(() => null);
  };

  // Snip: drag a rectangle over a PDF page; the region goes to the chat as an
  // image, with the text under it and where it comes from in the source.
  const [snipping, setSnipping] = useState(false);
  const [drag, setDrag] = useState<{ x0: number; y0: number; x1: number; y1: number; page: HTMLElement } | null>(null);
  useEffect(() => {
    if (!snipping) return;
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") { setSnipping(false); setDrag(null); } };
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [snipping]);
  const finishSnip = async (d: { x0: number; y0: number; x1: number; y1: number; page: HTMLElement }) => {
    setDrag(null);
    const box = d.page.getBoundingClientRect();
    const r = {
      left: Math.max(Math.min(d.x0, d.x1), box.left), right: Math.min(Math.max(d.x0, d.x1), box.right),
      top: Math.max(Math.min(d.y0, d.y1), box.top), bottom: Math.min(Math.max(d.y0, d.y1), box.bottom),
    };
    const canvas = d.page.querySelector("canvas");
    if (!canvas || r.right - r.left < 8 || r.bottom - r.top < 8) return; // a click, not a drag
    setSnipping(false);
    const c = canvas.getBoundingClientRect();
    const scale = canvas.width / c.width;
    const sw = (r.right - r.left) * scale, sh = (r.bottom - r.top) * scale;
    const shrink = Math.min(1, 2000 / Math.max(sw, sh));
    const out = document.createElement("canvas");
    out.width = Math.round(sw * shrink);
    out.height = Math.round(sh * shrink);
    const g = out.getContext("2d")!;
    g.fillStyle = "#fff";
    g.fillRect(0, 0, out.width, out.height);
    g.drawImage(canvas, (r.left - c.left) * scale, (r.top - c.top) * scale, sw, sh, 0, 0, out.width, out.height);
    const text = [...d.page.querySelectorAll<HTMLElement>(".textLayer span")].filter((span) => {
      const b = span.getBoundingClientRect();
      const x = b.left + b.width / 2, y = b.top + b.height / 2;
      return b.width > 0 && x >= r.left && x <= r.right && y >= r.top && y <= r.bottom;
    }).map((span) => span.textContent ?? "").join(" ").replace(/\s+/g, " ").trim().slice(0, 4000);
    const page = Number(d.page.dataset.page);
    const at = await sourceAt(page, d.page, (r.left + r.right) / 2, (r.top + r.bottom) / 2);
    pushQuote({ source: "pdf", page, text, image: out.toDataURL("image/png"), ...(at ? { path: at.path, line: at.line } : {}) });
  };

  const openFile = async (next: string) => {
    if (next !== path && !(await source.save())) return false;
    setPath(next);
    return true;
  };

  const toPdf = async () => {
    const editor = view.current;
    if (!editor || !build?.pdf) return;
    try {
      const line = editor.state.doc.lineAt(editor.state.selection.main.head).number;
      const at = await forwardSync(id, path, line);
      const size = pdf.current?.pageSize(at.page);
      pdf.current?.scrollTo(at.page, size ? Math.max(0, at.y - 30) / size.h : 0, true);
      setPane("pdf");
      setNotice("");
    } catch (e) { setNotice(errMsg(e)); }
  };

  const fromPdf = async (page: number, yFrac: number, xFrac: number, size: { w: number; h: number }) => {
    try {
      const at = await inverseSync(id, page, xFrac * size.w, yFrac * size.h);
      if (await openFile(at.path)) { setJump(at); setPane("editor"); }
    } catch (e) { setNotice(errMsg(e)); }
  };

  const selectedFolder = folderOf(path);
  const act = async (fn: () => Promise<unknown>) => {
    setNotice("");
    try { await fn(); await refresh(); setChanges((c) => c + 1); }
    catch (e) { setNotice(errMsg(e)); }
  };
  const addFile = async (directory: boolean) => {
    const name = await ask({
      title: directory ? "New folder" : "New file", input: selectedFolder, confirm: "Create",
      placeholder: directory ? "figures" : "sections/intro.tex",
    });
    if (!name) return;
    void act(async () => {
      await newFile(id, name, directory);
      if (!directory) await openFile(name);
    });
  };
  const upload = (ev: React.ChangeEvent<HTMLInputElement>, folder: boolean) => {
    const files = Array.from(ev.currentTarget.files ?? []);
    ev.currentTarget.value = "";
    if (!files.length) return;
    // A folder import keeps its tree (minus the chosen folder's own name).
    const paths = folder ? files.map((f) => f.webkitRelativePath.split("/").slice(1).join("/")) : undefined;
    void act(async () => {
      const r = await uploadFiles(id, files, selectedFolder, paths);
      if (r.errors.length) setNotice(r.errors.join("; "));
    });
  };
  const onTreeAction = async (action: "rename" | "delete" | "main", target: string, directory: boolean) => {
    if (!project) return;
    if (action === "main") {
      void act(() => saveSettings(id, { ...project.settings, main: target }));
    } else if (action === "delete") {
      const ok = await ask({
        title: `Delete ${leaf(target)}?`, confirm: "Delete", danger: true,
        message: directory ? `The folder ${target} and everything in it will be deleted.` : `${target} will be deleted.`,
      });
      if (ok === null) return;
      void act(async () => { await deleteFile(id, target); if (path === target || path.startsWith(target + "/")) setPath(project.settings.main); });
    } else {
      const to = await ask({
        title: `Rename ${directory ? "folder" : "file"}`, input: target, confirm: "Rename",
        message: "Include a folder to move it, e.g. sections/intro.tex.",
      });
      if (!to || to === target) return;
      void act(async () => {
        if (path === target && !(await source.save())) return;
        await renameFile(id, target, to);
        if (path === target) setPath(to);
      });
    }
  };
  const setEngine = (engine: Engine) => { if (project) void act(() => saveSettings(id, { ...project.settings, engine })); };

  if (loadError) return <p role="alert" className="text-red-600">{loadError} <Link to="/paper">Back to projects</Link></p>;
  if (!project) return <p className="text-sm text-slate-400">Loading…</p>;

  const running = compiling || !!build?.running;
  const pdfFile = build?.pdf ? pdfUrl(id, build.pdf) : null;

  return (
    <div className="ml-[calc(50%-50vw+1rem)] flex w-[calc(100vw-2rem)] flex-col border border-[var(--color-border)] bg-white"
      style={{ height: "calc(100vh - 8rem)" }}>
      {askDialog}
      {drag && (
        <div className="pointer-events-none fixed z-50 border-2 border-sky-500 bg-sky-400/10"
          style={{ left: Math.min(drag.x0, drag.x1), top: Math.min(drag.y0, drag.y1), width: Math.abs(drag.x1 - drag.x0), height: Math.abs(drag.y1 - drag.y0) }} />
      )}
      {offer && (
        <button ref={offerBtn} type="button" onMouseDown={(e) => e.preventDefault()} onClick={() => void addQuote()}
          className="fixed z-50 flex items-center gap-1 border border-[var(--color-border)] bg-white px-2 py-1 text-xs text-slate-700 shadow-md hover:bg-sky-50 hover:text-slate-900"
          style={{ left: Math.max(8, Math.min(offer.x - 40, window.innerWidth - 120)), top: Math.max(8, Math.min(offer.y + 6, window.innerHeight - 36)) }}>
          <MessageSquarePlus className="h-3.5 w-3.5 text-sky-600" /> Add to chat
        </button>
      )}
      <header className="flex shrink-0 items-center gap-3 border-b border-[var(--color-border)] px-3 py-1.5 text-sm">
        <Link to="/paper" className="flex items-center gap-1 text-slate-500 no-underline hover:text-slate-900">
          <ArrowLeft className="h-4 w-4" /> Projects
        </Link>
        <span className="text-slate-300">/</span>
        <strong className="min-w-0 truncate font-normal text-[var(--color-heading)]" title={project.meta.title}>{project.meta.title}</strong>
        <span className="flex-1" />
        <span className={`text-xs ${live ? "text-emerald-600" : "text-amber-600"}`}>{live ? "Live" : "Reconnecting…"}</span>
        <a href={zipUrl(id)} className="flex items-center gap-1 text-slate-600 no-underline hover:text-slate-900" title="Download the project as a zip">
          <Download className="h-4 w-4" /> Zip
        </a>
      </header>
      {!wide && (
        <nav className="flex shrink-0 border-b border-[var(--color-border)] text-xs" aria-label="Panes">
          {([["files", "Files"], ["agent", "Agent"], ["editor", "Editor"], ["pdf", "PDF"]] as const).map(([key, label]) => {
            const on = key === "files" || key === "agent" ? pane === "side" && left === key : pane === key;
            return (
              <button key={key} type="button" aria-pressed={on}
                className={`flex-1 py-2 ${on ? "border-b-2 border-[var(--color-primary)] text-[var(--color-heading)]" : "text-slate-500"}`}
                onClick={() => { if (key === "files" || key === "agent") { setLeft(key); setPane("side"); } else setPane(key); }}>
                {label}{key === "agent" && agent.running ? " •" : ""}
              </button>
            );
          })}
        </nav>
      )}
      <div ref={grid} className="grid min-h-0 flex-1"
        style={{ gridTemplateColumns: wide
          ? `${left === "agent" ? cols.agent : cols.files}px 0.5rem minmax(0,${cols.split}fr) 0.5rem minmax(0,${1 - cols.split}fr)`
          : "minmax(0,1fr)" }}>
        <aside className={`min-h-0 flex-col ${wide || pane === "side" ? "flex" : "hidden"}`}>
          <div className={`shrink-0 border-b border-[var(--color-border)] text-xs ${wide ? "flex" : "hidden"}`}>
            {([["files", "Files", Folder], ["agent", "Agent", Bot]] as const).map(([key, label, Icon]) => (
              <button key={key} type="button" aria-pressed={left === key} onClick={() => setLeft(key)}
                className={`flex flex-1 items-center justify-center gap-1.5 py-2 ${left === key ? "border-b-2 border-[var(--color-primary)] text-[var(--color-heading)]" : "text-slate-500 hover:text-slate-800"}`}>
                <Icon className="h-3.5 w-3.5" /> {label}
                {key === "agent" && agent.running && <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-sky-500" aria-label="working" />}
              </button>
            ))}
          </div>
          {left === "files" ? (
            <>
              <div className="flex shrink-0 items-center gap-0.5 border-b border-[var(--color-border)] px-1.5 py-1">
                <button type="button" className={toolBtn} title="New file" aria-label="New file" onClick={() => void addFile(false)}><FilePlus className="h-4 w-4" /></button>
                <button type="button" className={toolBtn} title="New folder" aria-label="New folder" onClick={() => void addFile(true)}><FolderPlus className="h-4 w-4" /></button>
                <button type="button" className={toolBtn} title="Upload files" aria-label="Upload files" onClick={() => chooser.current?.click()}><Upload className="h-4 w-4" /></button>
                <button type="button" className={toolBtn} title="Upload a folder" aria-label="Upload a folder" onClick={() => folderChooser.current?.click()}><FolderUp className="h-4 w-4" /></button>
                <input ref={chooser} hidden type="file" multiple onChange={(e) => upload(e, false)} />
                <input ref={folderChooser} hidden type="file" multiple {...{ webkitdirectory: "" }} onChange={(e) => upload(e, true)} />
              </div>
              <FileTree project={project} selected={path} onOpen={(p) => void openFile(p).then((ok) => ok && setPane("editor"))}
                onAction={(a, t, d) => void onTreeAction(a, t, d)} />
            </>
          ) : (
            <PaperAgent id={id} version={agent.version} running={agent.running} context={agentContext}
              quotes={quotes} onQuotes={setQuotes}
              onOpenFile={(p, line) => void openFile(p).then((ok) => { if (ok) setPane("editor"); if (ok && line) setJump({ path: p, line }); })} />
          )}
        </aside>
        {wide && (
          <Splitter label="Resize the sidebar" onDrag={(x) => {
            const box = grid.current!.getBoundingClientRect();
            const max = left === "agent" ? 720 : 480;
            saveCols({ ...cols, [left]: Math.round(clamp(x - box.left, left === "agent" ? 300 : 170, Math.min(max, box.width * 0.5))) });
          }} />
        )}

        <section className={`min-h-0 flex-col ${wide || pane === "editor" ? "flex" : "hidden"}`} aria-label="Editor">
          <div className="flex shrink-0 items-center gap-1 border-b border-[var(--color-border)] px-2 py-1 text-xs">
            <span className="flex min-w-0 flex-1 items-center gap-1.5 truncate text-slate-600" title={path}>
              <FileIcon className="h-3.5 w-3.5 shrink-0" /> {path}
            </span>
            <button type="button" className={toolBtn} title="Find and replace" aria-label="Find and replace" disabled={!source.current}
              onClick={() => view.current && openSearchPanel(view.current)}><Search className="h-4 w-4" /></button>
            <button type="button" className={toolBtn} title="Show this line in the PDF" aria-label="Show this line in the PDF"
              disabled={!source.current || !build?.pdf} onClick={() => void toPdf()}><Crosshair className="h-4 w-4" /></button>
            <button type="button" className={toolBtn} title="Reload from disk" aria-label="Reload from disk"
              onClick={() => { void refresh(); void source.reload(); }}><RefreshCw className="h-4 w-4" /></button>
          </div>
          {(notice || source.error || source.conflict) && (
            <div role="status" className="shrink-0 space-y-1 border-b border-amber-200 bg-amber-50 px-3 py-1.5 text-xs text-amber-800">
              {notice && <div className="flex gap-2"><span className="flex-1">{notice}</span><button type="button" className="underline" onClick={() => setNotice("")}>Dismiss</button></div>}
              {source.conflict ? (
                <div className="flex flex-wrap items-center gap-2">
                  This file changed on disk while you were editing it.
                  <button type="button" className="underline" onClick={source.takeDisk}>Use the disk version</button>
                  <button type="button" className="underline" onClick={() => void source.save(source.conflict!)}>Keep my edits</button>
                </div>
              ) : source.error && <div>{source.error}</div>}
            </div>
          )}
          <div className="min-h-0 flex-1">
            {source.current ? (
              <SourceEditor path={path} text={source.current.text} onChange={source.edit} onSave={() => void source.save()} viewRef={view} />
            ) : IMAGE.test(path) ? (
              <div className="flex h-full items-center justify-center overflow-auto bg-slate-50 p-4">
                <img alt={leaf(path)} src={`${assetUrl(id, path)}&v=${assetVersion}`} className="max-h-full max-w-full" />
              </div>
            ) : EDITABLE.test(path) ? (
              <p className="p-6 text-sm text-slate-400">{source.error ? "This file cannot be opened." : "Opening…"}</p>
            ) : path ? (
              <p className="p-6 text-sm text-slate-500">
                No preview for this file. <a href={assetUrl(id, path)} download={leaf(path)}>Download {leaf(path)}</a>
              </p>
            ) : null}
          </div>
          <footer className="shrink-0 border-t border-[var(--color-border)] px-3 py-1 text-[11px] text-slate-400">
            {source.saving ? "Saving…" : source.dirty ? "Unsaved changes" : source.current ? "Saved" : " "}
          </footer>
        </section>
        {wide && (
          <Splitter label="Resize the editor and the PDF" onDrag={(x) => {
            const box = grid.current!.getBoundingClientRect();
            const side = left === "agent" ? cols.agent : cols.files;
            const rest = box.width - side - 16;
            saveCols({ ...cols, split: clamp((x - box.left - side - 8) / rest, 0.2, 0.8) });
          }} />
        )}

        <section className={`min-h-0 flex-col ${wide || pane === "pdf" ? "flex" : "hidden"}`} aria-label="PDF preview">
          <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-[var(--color-border)] px-2 py-1 text-xs">
            <button type="button" className="btn-primary px-3 py-1 text-xs" disabled={running || !!source.conflict} onClick={() => void runCompile()}>
              {running ? "Compiling…" : "Recompile"}
            </button>
            <label className="flex items-center gap-1 text-slate-600">
              <input type="checkbox" checked={auto} onChange={(e) => { setAuto(e.target.checked); localStorage.setItem(AUTO_KEY, e.target.checked ? "on" : "off"); }} />
              Auto
            </label>
            {project.compiler === "tectonic" ? (
              <span className="text-slate-500" title="TeXDeck's built-in TeX (XeTeX). Install TeX Live, MacTeX or MiKTeX to choose pdflatex or lualatex.">
                Tectonic
              </span>
            ) : (
              <select aria-label="Compiler" value={project.settings.engine} onChange={(e) => setEngine(e.target.value as Engine)}
                className="border border-[var(--color-border)] bg-white px-1 py-0.5">
                {ENGINES.map((x) => <option key={x}>{x}</option>)}
              </select>
            )}
            <button type="button" aria-pressed={showLog} className={`${toolBtn} ${showLog ? "text-slate-900" : ""}`} disabled={!build?.log}
              onClick={() => setShowLog((v) => !v)}>Log</button>
            <button type="button" aria-pressed={snipping} disabled={!pdfFile || showLog}
              title={snipping ? "Drag over the PDF to snip a region into the chat (Esc to cancel)" : "Snip a region of the PDF into the chat"}
              className={`${toolBtn} ${snipping ? "bg-sky-100 text-sky-800" : ""}`} onClick={() => setSnipping((v) => !v)}>
              <Scissors className="h-4 w-4" /> Snip
            </button>
            <span className="flex-1" />
            {build?.pdf && (
              <a href={`${pdfUrl(id, build.pdf)}`} download className={`${toolBtn} no-underline`} title="Download the PDF">
                <Download className="h-4 w-4" /> PDF
              </a>
            )}
          </div>
          <div className="relative flex min-h-0 flex-1 flex-col">
          {build && !build.running && !build.ok && build.error && (
            <div role="alert" className="absolute inset-x-3 bottom-3 z-20 flex max-h-32 items-start gap-2 overflow-auto border border-red-200 bg-red-50/95 px-3 py-2 text-xs text-red-700 shadow-lg">
              <span className="min-w-0 flex-1 break-words">
                <span className="font-mono">{build.error}</span>
                {build.pdf && <span className="ml-1 text-red-500/80">The PDF below is the last build that worked.</span>}
              </span>
              {build.log && <button type="button" className="shrink-0 underline" onClick={() => setShowLog(true)}>Log</button>}
            </div>
          )}
          {showLog && build?.log ? (
            <pre ref={(el) => { if (el && !el.dataset.scrolled) { el.scrollTop = el.scrollHeight; el.dataset.scrolled = "1"; } }}
              className="min-h-0 flex-1 overflow-auto whitespace-pre-wrap bg-slate-900 p-3 font-mono text-[11px] text-slate-100">{build.log}</pre>
          ) : (
            <div className={`min-h-0 flex-1 bg-slate-100${snipping ? " cursor-crosshair select-none [&_.textLayer]:pointer-events-none" : ""}`}
              onMouseDownCapture={(e) => {
                if (!snipping || e.button !== 0) return;
                const page = (e.target as HTMLElement).closest<HTMLElement>("[data-page]");
                if (!page) return;
                e.preventDefault();
                e.stopPropagation();
                let d = { x0: e.clientX, y0: e.clientY, x1: e.clientX, y1: e.clientY, page };
                setDrag(d);
                const move = (m: MouseEvent) => { d = { ...d, x1: m.clientX, y1: m.clientY }; setDrag(d); };
                const up = () => {
                  window.removeEventListener("mousemove", move);
                  window.removeEventListener("mouseup", up);
                  void finishSnip(d);
                };
                window.addEventListener("mousemove", move);
                window.addEventListener("mouseup", up);
              }}
              onDoubleClickCapture={(e) => { if (snipping) e.stopPropagation(); }}>
              {pdfFile ? (
                <PdfScroll ref={pdf} file={pdfFile} label={leaf(project.settings.main).replace(/\.tex$/i, ".pdf")} keepScroll
                  hint="Double-click the PDF to jump to its source line" onDoubleClick={(p, y, x, size) => void fromPdf(p, y, x, size)} />
              ) : (
                <div className="flex h-full items-center justify-center text-sm text-slate-500">
                  {running ? "Building your PDF…" : build?.error ? "The project does not compile yet." : "Your PDF will appear here."}
                </div>
              )}
            </div>
          )}
          </div>
        </section>
      </div>
    </div>
  );
}
