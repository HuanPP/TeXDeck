import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { ArrowLeft, ImageUp, RotateCcw, Trash2 } from "lucide-react";
import {
  assetUrl,
  createCustomTemplate,
  deleteCustomTemplate,
  getCustomTemplate,
  listTemplateBases,
  previewTemplateDraft,
  templateImageUrl,
  templateLayout,
  updateCustomTemplate,
  uploadTemplateImage,
} from "../api";
import PdfViewer from "../components/PdfViewer";
import BackgroundEditor from "../components/BackgroundEditor";
import { type BackgroundState, type LogoBackground, prepareSource, processLogo } from "../imageTools";
import Segmented from "../components/Segmented";
import type { ImageSlot, LayoutBox, TemplateBase, TemplateDraft } from "../types";

type LogoSource = { source: string; background: LogoBackground };
type EditorState = {
  background?: BackgroundState;
  logos?: Partial<Record<ImageSlot, LogoSource>>;
};

const PREVIEW_DELAY_MS = 600;

const errorText = (e: unknown) => (e instanceof Error ? e.message : String(e));

export default function TemplateEditor() {
  const { key } = useParams(); // undefined while creating
  const [search] = useSearchParams();
  const navigate = useNavigate();

  const [bases, setBases] = useState<TemplateBase[]>([]);
  const [baseKey, setBaseKey] = useState("");
  const [name, setName] = useState("");
  const [colors, setColors] = useState<Record<string, string>>({});
  const [images, setImages] = useState<Partial<Record<ImageSlot, string>>>({});
  const [editor, setEditor] = useState<EditorState>({});
  const [loadError, setLoadError] = useState("");
  const [dirty, setDirty] = useState(false);

  const [pdf, setPdf] = useState<string | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [previewError, setPreviewError] = useState("");
  const [layout, setLayout] = useState<LayoutBox[]>([]);
  const [bgOpen, setBgOpen] = useState(false);
  const [logoBusy, setLogoBusy] = useState<ImageSlot | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [savedAt, setSavedAt] = useState("");

  const base = bases.find((b) => b.key === baseKey);

  // Load the bases, then the saved template (or start from ?base=).
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const list = await listTemplateBases();
        if (!alive) return;
        setBases(list);
        if (key) {
          const spec = await getCustomTemplate(key);
          if (!alive) return;
          const b = list.find((x) => x.key === spec.base);
          setBaseKey(spec.base);
          setName(spec.name);
          setColors({ ...Object.fromEntries((b?.colors ?? []).map((c) => [c.name, c.default])), ...spec.colors });
          setImages(spec.images as Partial<Record<ImageSlot, string>>);
          setEditor(spec.editor as EditorState);
        } else {
          const wanted = search.get("base");
          const b = list.find((x) => x.key === wanted) ?? list[0];
          if (b) chooseBase(b);
        }
      } catch (e) {
        if (alive) setLoadError(errorText(e));
      }
    })();
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  function chooseBase(b: TemplateBase) {
    setBaseKey(b.key);
    setName((n) => n || `My ${b.name}`);
    setColors(Object.fromEntries(b.colors.map((c) => [c.name, c.default])));
    setImages({});
    setEditor({});
  }

  useEffect(() => {
    if (!baseKey) return;
    templateLayout(baseKey).then(setLayout, () => setLayout([]));
  }, [baseKey]);

  const draft: TemplateDraft | null = useMemo(
    () => (baseKey ? { base: baseKey, name, colors, images, editor: editor as Record<string, unknown> } : null),
    [baseKey, name, colors, images, editor],
  );

  // Live preview: rebuild the sample deck a moment after the last change.
  // The name and editor state do not change the PDF, so they do not trigger it.
  const previewKey = draft ? JSON.stringify([draft.base, draft.colors, draft.images]) : "";
  const pdfRef = useRef<string | null>(null);
  useEffect(() => {
    if (!draft) return;
    const ctrl = new AbortController();
    const timer = window.setTimeout(async () => {
      setPreviewing(true);
      try {
        const blob = await previewTemplateDraft(draft, ctrl.signal);
        const url = URL.createObjectURL(blob);
        if (pdfRef.current) URL.revokeObjectURL(pdfRef.current);
        pdfRef.current = url;
        setPdf(url);
        setPreviewError("");
      } catch (e) {
        if (!ctrl.signal.aborted) setPreviewError(errorText(e));
      } finally {
        if (!ctrl.signal.aborted) setPreviewing(false);
      }
    }, PREVIEW_DELAY_MS);
    return () => {
      window.clearTimeout(timer);
      ctrl.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [previewKey]);
  useEffect(() => () => {
    if (pdfRef.current) URL.revokeObjectURL(pdfRef.current);
  }, []);

  useEffect(() => {
    if (!dirty) return;
    const warn = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  const touch = () => {
    setDirty(true);
    setSavedAt("");
  };

  function setColor(slot: string, value: string) {
    setColors((c) => ({ ...c, [slot]: value }));
    touch();
  }

  function setImage(slot: ImageSlot, id: string | null) {
    setImages((m) => {
      const next = { ...m };
      if (id) next[slot] = id;
      else delete next[slot];
      return next;
    });
    touch();
  }

  async function makeLogo(slot: ImageSlot, src: LogoSource) {
    const before = editor.logos?.[slot];
    // Show the choice at once; the processed logo follows.
    setEditor((e) => ({ ...e, logos: { ...e.logos, [slot]: src } }));
    setLogoBusy(slot);
    try {
      const blob = await processLogo(assetUrl(src.source), src.background);
      const { id } = await uploadTemplateImage(blob);
      setImage(slot, id);
    } catch (e) {
      setEditor((ed) => ({ ...ed, logos: { ...ed.logos, [slot]: before } }));
      setPreviewError(errorText(e));
    } finally {
      setLogoBusy(null);
    }
  }

  async function pickLogo(slot: ImageSlot, file: File) {
    setLogoBusy(slot);
    try {
      const { id } = await uploadTemplateImage(await prepareSource(file));
      await makeLogo(slot, { source: id, background: "keep" });
    } catch (e) {
      setPreviewError(errorText(e));
      setLogoBusy(null);
    }
  }

  function resetImage(slot: ImageSlot) {
    setImage(slot, null);
    setEditor((e) => {
      const next = { ...e, logos: { ...e.logos } };
      if (slot === "background") delete next.background;
      else delete next.logos[slot];
      return next;
    });
  }

  async function save() {
    if (!draft) return;
    setSaving(true);
    setSaveError("");
    try {
      const spec = key ? await updateCustomTemplate(key, draft) : await createCustomTemplate(draft);
      setDirty(false);
      setSavedAt(new Date().toLocaleTimeString());
      if (!key) navigate(`/templates/${spec.key}`, { replace: true });
    } catch (e) {
      setSaveError(errorText(e));
    } finally {
      setSaving(false);
    }
  }

  async function remove() {
    if (!key || !window.confirm(`Delete the template "${name}"? Decks already made with it are kept.`)) return;
    try {
      await deleteCustomTemplate(key);
      setDirty(false);
      navigate("/");
    } catch (e) {
      setSaveError(errorText(e));
    }
  }

  if (loadError) {
    return (
      <div className="app-surface p-6">
        <p className="text-red-600">{loadError}</p>
        <Link to="/" className="mt-3 inline-block text-[var(--color-primary)]">Back</Link>
      </div>
    );
  }

  const imageSlots = Object.entries(base?.images ?? {}) as [ImageSlot, string][];
  const mainColor = colors.maincolor ?? "#334155";

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <Link to="/" aria-label="Back" className="text-slate-500 hover:text-slate-800"><ArrowLeft size={18} /></Link>
          <h1 className="text-2xl">{key ? "Edit template" : "New template"}</h1>
        </div>
        <div className="flex items-center gap-2">
          {savedAt && !dirty && <span className="text-sm text-slate-500">Saved {savedAt}</span>}
          {key && !dirty && (
            <Link to={`/?template=${encodeURIComponent(key)}`} className="btn-secondary no-underline">Make a deck with it</Link>
          )}
          <button type="button" className="btn-primary" disabled={!draft || saving || (!!key && !dirty)} onClick={() => void save()}>
            {saving ? "Saving…" : key ? "Save changes" : "Save template"}
          </button>
        </div>
      </div>
      {saveError && <p className="text-sm text-red-600">{saveError}</p>}

      <div className="grid min-h-[70vh] gap-4 lg:grid-cols-[1fr_320px]">
        <section className="app-surface relative flex min-h-[60vh] min-w-0 flex-col p-3">
          {(previewing || previewError) && (
            <div className={`mb-2 text-sm ${previewError ? "text-red-600" : "text-slate-500"}`}>
              {previewError ? <span className="whitespace-pre-wrap">{previewError.slice(0, 600)}</span> : "Updating preview…"}
            </div>
          )}
          {pdf ? <PdfViewer file={pdf} /> : (
            <div className="flex flex-1 items-center justify-center text-slate-500">Building the preview…</div>
          )}
        </section>

        <aside className="app-surface flex flex-col gap-5 p-4 text-sm">
          <div>
            <label htmlFor="tpl-name" className="mb-1 block text-slate-600">Name</label>
            <input id="tpl-name" className="input-field" value={name} maxLength={60}
              onChange={(e) => { setName(e.target.value); touch(); }} />
          </div>

          <div>
            <label htmlFor="tpl-base" className="mb-1 block text-slate-600">Based on</label>
            <select id="tpl-base" className="input-field" value={baseKey} disabled={!!key}
              onChange={(e) => { const b = bases.find((x) => x.key === e.target.value); if (b) { chooseBase(b); touch(); } }}>
              {bases.map((b) => <option key={b.key} value={b.key}>{b.name}</option>)}
            </select>
            {!key && <p className="mt-1 text-xs text-slate-500">The layout comes from this template; changing it resets colours and images.</p>}
          </div>

          {base && (
            <div>
              <span className="mb-2 block text-slate-600">Colours</span>
              <div className="space-y-2">
                {base.colors.map((c) => (
                  <div key={c.name} className="flex items-center gap-2">
                    <input type="color" aria-label={c.label} value={colors[c.name] ?? c.default}
                      className="h-8 w-10 cursor-pointer border border-[var(--color-border)] bg-white p-0.5"
                      onChange={(e) => setColor(c.name, e.target.value)} />
                    <span className="flex-1">{c.label}</span>
                    <input aria-label={`${c.label} hex`} className="w-[5.5rem] border border-[var(--color-border)] px-2 py-1 font-mono text-xs uppercase"
                      defaultValue={colors[c.name] ?? c.default} key={colors[c.name]}
                      onBlur={(e) => { const v = e.target.value.trim(); if (/^#?[0-9a-fA-F]{6}$/.test(v)) setColor(c.name, v.startsWith("#") ? v : `#${v}`); }} />
                    <button type="button" title="Default" aria-label={`Reset ${c.label}`}
                      disabled={(colors[c.name] ?? c.default).toUpperCase() === c.default.toUpperCase()}
                      className="text-slate-400 hover:text-slate-700 disabled:opacity-30"
                      onClick={() => setColor(c.name, c.default)}>
                      <RotateCcw size={14} />
                    </button>
                  </div>
                ))}
              </div>
            </div>
          )}

          {imageSlots.map(([slot, label]) => {
            const custom = images[slot];
            const src = custom ? assetUrl(custom) : templateImageUrl(baseKey, slot);
            const logo = editor.logos?.[slot];
            return (
              <div key={slot}>
                <span className="mb-2 block text-slate-600">{label}</span>
                <div className="flex items-center gap-3">
                  <div className={`flex shrink-0 items-center justify-center overflow-hidden border border-[var(--color-border)] ${
                    slot === "background" ? "aspect-video w-32" : "h-16 w-24 p-1.5"}`}
                    style={{ background: slot === "trans_logo" ? mainColor : "white" }}>
                    <img src={src} alt="" className={slot === "background" ? "h-full w-full object-cover" : "max-h-full max-w-full object-contain"} />
                  </div>
                  <div className="flex flex-col items-start gap-1">
                    {slot === "background" ? (
                      <button type="button" className="inline-flex items-center gap-1 text-[var(--color-primary)] hover:underline"
                        onClick={() => setBgOpen(true)}>
                        <ImageUp size={14} /> {custom ? "Edit" : "Use a picture"}
                      </button>
                    ) : (
                      <label className="inline-flex cursor-pointer items-center gap-1 text-[var(--color-primary)] hover:underline">
                        <ImageUp size={14} /> {logoBusy === slot ? "Working…" : "Upload"}
                        <input type="file" accept="image/png,image/jpeg,image/webp" hidden disabled={logoBusy !== null}
                          onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ""; if (f) void pickLogo(slot, f); }} />
                      </label>
                    )}
                    {custom && (
                      <button type="button" className="text-slate-500 hover:text-slate-800" onClick={() => resetImage(slot)}>
                        Back to the template's
                      </button>
                    )}
                  </div>
                </div>
                {logo && (
                  <div className="mt-2">
                    <span className="mb-1 block text-xs text-slate-500">Plain background</span>
                    <Segmented<LogoBackground> label={`${label}: plain background`} value={logo.background}
                      disabled={logoBusy !== null}
                      onChange={(background) => void makeLogo(slot, { ...logo, background })}
                      options={[
                        { value: "keep", label: "Keep" },
                        { value: "outside", label: "Around it" },
                        { value: "all", label: "Everywhere" },
                      ]} />
                  </div>
                )}
                {slot === "trans_logo" && (
                  <p className="mt-1 text-xs text-slate-500">Sits on the main colour: a white or light version reads best.</p>
                )}
              </div>
            );
          })}

          {key && (
            <button type="button" className="mt-auto inline-flex items-center gap-1.5 self-start text-red-600 hover:underline"
              onClick={() => void remove()}>
              <Trash2 size={14} /> Delete template
            </button>
          )}
        </aside>
      </div>

      {bgOpen && base && (
        <BackgroundEditor
          initial={editor.background ?? null}
          templateBackground={templateImageUrl(baseKey, "background")}
          layout={layout}
          mainColor={mainColor}
          onClose={() => setBgOpen(false)}
          onApply={(id, state) => {
            setImage("background", id);
            setEditor((e) => ({ ...e, background: state }));
            setBgOpen(false);
          }}
        />
      )}
    </div>
  );
}
