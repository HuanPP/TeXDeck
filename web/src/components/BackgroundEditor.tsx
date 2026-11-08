import { useEffect, useRef, useState } from "react";
import { Lock, Unlock, X } from "lucide-react";
import Segmented from "./Segmented";
import { assetUrl, uploadTemplateImage } from "../api";
import type { LayoutBox } from "../types";
import {
  SLIDE_H,
  SLIDE_W,
  type BackgroundState,
  type FillMode,
  drawBackground,
  exportBackground,
  fitLayer,
  loadImage,
  prepareSource,
} from "../imageTools";

type Layer = BackgroundState["layer"];
type Handle = "move" | "rotate" | "n" | "s" | "e" | "w" | "nw" | "ne" | "sw" | "se";
type Drag = { handle: Handle; start: { x: number; y: number }; layer: Layer; shift: boolean };

const MIN_SIDE = 24;
const HANDLES: { h: Handle; x: number; y: number }[] = [
  { h: "nw", x: -1, y: -1 }, { h: "n", x: 0, y: -1 }, { h: "ne", x: 1, y: -1 },
  { h: "e", x: 1, y: 0 }, { h: "se", x: 1, y: 1 }, { h: "s", x: 0, y: 1 },
  { h: "sw", x: -1, y: 1 }, { h: "w", x: -1, y: 0 },
];
const CURSOR: Record<string, string> = { n: "ns-resize", s: "ns-resize", e: "ew-resize", w: "ew-resize", nw: "nwse-resize", se: "nwse-resize", ne: "nesw-resize", sw: "nesw-resize" };

const rad = (deg: number) => (deg * Math.PI) / 180;

// Rotate v by deg (slide pixels).
function rotate(v: { x: number; y: number }, deg: number) {
  const c = Math.cos(rad(deg));
  const s = Math.sin(rad(deg));
  return { x: v.x * c - v.y * s, y: v.x * s + v.y * c };
}

// New layer for a drag of `handle` from drag.start to p (slide pixels).
function dragLayer(drag: Drag, p: { x: number; y: number }, lockRatio: boolean, shift: boolean): Layer {
  const L = drag.layer;
  const dx = p.x - drag.start.x;
  const dy = p.y - drag.start.y;
  if (drag.handle === "move") return { ...L, cx: L.cx + dx, cy: L.cy + dy };

  if (drag.handle === "rotate") {
    const a0 = Math.atan2(drag.start.y - L.cy, drag.start.x - L.cx);
    const a1 = Math.atan2(p.y - L.cy, p.x - L.cx);
    let rot = L.rot + ((a1 - a0) * 180) / Math.PI;
    if (shift) rot = Math.round(rot / 15) * 15;
    else {
      const near = Math.round(rot / 45) * 45; // snap to 0/45/90… when close
      if (Math.abs(rot - near) < 3) rot = near;
    }
    rot = ((rot % 360) + 540) % 360 - 180;
    return { ...L, rot };
  }

  // Resize in the layer's own frame; the opposite side/corner stays put.
  const hx = drag.handle.includes("e") ? 1 : drag.handle.includes("w") ? -1 : 0;
  const hy = drag.handle.includes("s") ? 1 : drag.handle.includes("n") ? -1 : 0;
  const local = rotate({ x: dx, y: dy }, -L.rot);
  let w = hx ? Math.max(MIN_SIDE, L.w + hx * local.x) : L.w;
  let h = hy ? Math.max(MIN_SIDE, L.h + hy * local.y) : L.h;
  // Corners keep the proportions unless unlocked (Shift flips the lock for one drag).
  if (hx && hy && lockRatio !== shift) {
    const f = Math.max(w / L.w, h / L.h);
    w = Math.max(MIN_SIDE, L.w * f);
    h = Math.max(MIN_SIDE, L.h * f);
  }
  const shift_ = rotate({ x: (hx * (w - L.w)) / 2, y: (hy * (h - L.h)) / 2 }, L.rot);
  return { ...L, w, h, cx: L.cx + shift_.x, cy: L.cy + shift_.y };
}

type Props = {
  initial: BackgroundState | null; // editor state saved with the template
  templateBackground: string; // URL of the base pack's own background
  layout: LayoutBox[]; // title-page text and logo boxes, as guides
  mainColor: string;
  onApply: (assetId: string, state: BackgroundState) => void;
  onClose: () => void;
};

export default function BackgroundEditor(props: Props) {
  const [state, setState] = useState<BackgroundState | null>(props.initial);
  const [picture, setPicture] = useState<HTMLImageElement | null>(null);
  const [templateBg, setTemplateBg] = useState<HTMLImageElement | null>(null);
  const [lockRatio, setLockRatio] = useState(true);
  const [guides, setGuides] = useState(true);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [scale, setScale] = useState(0.4); // screen px per slide px
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const dragRef = useRef<Drag | null>(null);

  useEffect(() => {
    loadImage(props.templateBackground).then(setTemplateBg, () => setTemplateBg(null));
  }, [props.templateBackground]);

  // Reopening a saved template: load its original picture again.
  useEffect(() => {
    if (!props.initial) return;
    loadImage(assetUrl(props.initial.source)).then(setPicture, () => setError("The saved picture is gone; upload it again"));
  }, [props.initial]);

  useEffect(() => {
    const stage = stageRef.current;
    if (!stage) return;
    const ro = new ResizeObserver(() => setScale(stage.clientWidth / SLIDE_W));
    ro.observe(stage);
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    const ctx = canvasRef.current?.getContext("2d");
    if (ctx && state && picture) drawBackground(ctx, state, picture, templateBg);
  }, [state, picture, templateBg]);

  async function pick(file: File) {
    setError("");
    setBusy("Uploading…");
    try {
      const { id } = await uploadTemplateImage(await prepareSource(file));
      const img = await loadImage(assetUrl(id));
      setPicture(img);
      setState((prev) => ({
        source: id,
        layer: fitLayer(img, false),
        fill: prev?.fill ?? { mode: "blur", color: "#ffffff" },
        overlay: prev?.overlay ?? { color: "#000000", opacity: 0 },
      }));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("");
    }
  }

  function toSlide(e: { clientX: number; clientY: number }) {
    const r = stageRef.current!.getBoundingClientRect();
    return { x: (e.clientX - r.left) / scale, y: (e.clientY - r.top) / scale };
  }

  function startDrag(handle: Handle) {
    return (e: React.PointerEvent) => {
      if (!state) return;
      e.preventDefault();
      e.stopPropagation();
      (e.target as Element).setPointerCapture(e.pointerId);
      dragRef.current = { handle, start: toSlide(e), layer: state.layer, shift: e.shiftKey };
    };
  }

  function onPointerMove(e: React.PointerEvent) {
    const drag = dragRef.current;
    if (!drag || !state) return;
    const layer = dragLayer(drag, toSlide(e), lockRatio, e.shiftKey);
    setState({ ...state, layer });
  }

  // The wheel zooms the picture; a native listener so the page does not scroll.
  useEffect(() => {
    const stage = stageRef.current;
    if (!stage) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const f = Math.exp(-e.deltaY * 0.001);
      setState((s) => {
        if (!s || s.layer.w * f < MIN_SIDE || s.layer.h * f < MIN_SIDE) return s;
        return { ...s, layer: { ...s.layer, w: s.layer.w * f, h: s.layer.h * f } };
      });
    };
    stage.addEventListener("wheel", onWheel, { passive: false });
    return () => stage.removeEventListener("wheel", onWheel);
  }, []);

  async function apply() {
    if (!state || !picture) return;
    setBusy("Saving…");
    setError("");
    try {
      const blob = await exportBackground(state, picture, templateBg);
      const { id } = await uploadTemplateImage(blob);
      props.onApply(id, state);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy("");
    }
  }

  const set = (patch: Partial<BackgroundState>) => state && setState({ ...state, ...patch });
  const L = state?.layer;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-4" role="dialog" aria-label="Background editor">
      <div className="app-surface flex max-h-full w-full max-w-6xl flex-col overflow-auto">
        <div className="flex items-center justify-between border-b border-[var(--color-border)] px-4 py-3">
          <h2 className="text-lg">Title page background</h2>
          <button type="button" aria-label="Close" onClick={props.onClose} className="text-slate-500 hover:text-slate-800">
            <X size={18} />
          </button>
        </div>

        <div className="grid gap-4 p-4 lg:grid-cols-[1fr_17rem]">
          <div>
            {/* Margin around the slide so handles and the rotate knob stay reachable at its edges. */}
            <div className="overflow-hidden bg-slate-100 px-6 pb-6 pt-11"
              onPointerMove={onPointerMove} onPointerUp={() => (dragRef.current = null)}>
            <div ref={stageRef} className="relative aspect-video w-full touch-none select-none border border-[var(--color-border)] bg-[repeating-conic-gradient(#e2e8f0_0_25%,#fff_0_50%)] bg-[length:20px_20px]">
              <canvas ref={canvasRef} width={SLIDE_W} height={SLIDE_H} className="absolute inset-0 h-full w-full" />
              {!state && (
                <button type="button" className="absolute inset-0 flex flex-col items-center justify-center gap-2 text-slate-600"
                  onClick={() => fileRef.current?.click()}>
                  <span className="btn-primary">Choose a picture</span>
                  <span className="text-sm">Any size or shape: place it on the slide afterwards</span>
                </button>
              )}
              {guides && state && props.layout.map((b, i) => (
                <div key={i} className="pointer-events-none absolute border border-dashed"
                  style={{
                    left: `${b.x * 100}%`, top: `${b.y * 100}%`, width: `${b.w * 100}%`, height: `${b.h * 100}%`,
                    borderColor: "rgb(14 165 233 / 0.9)", background: "rgb(14 165 233 / 0.10)",
                  }} />
              ))}
              {state && L && picture && (
                <div className="absolute cursor-move outline outline-1 outline-sky-500"
                  onPointerDown={startDrag("move")}
                  style={{
                    left: (L.cx - L.w / 2) * scale, top: (L.cy - L.h / 2) * scale,
                    width: L.w * scale, height: L.h * scale, transform: `rotate(${L.rot}deg)`,
                  }}>
                  {HANDLES.map(({ h, x, y }) => (
                    <span key={h} onPointerDown={startDrag(h)}
                      className={`absolute h-3 w-3 border border-sky-600 bg-white ${x && y ? "" : "rounded-full"}`}
                      style={{ left: `calc(${(x + 1) * 50}% - 6px)`, top: `calc(${(y + 1) * 50}% - 6px)`, cursor: CURSOR[h] }} />
                  ))}
                  <span className="absolute left-1/2 top-[-28px] h-[22px] w-px bg-sky-500" />
                  <span onPointerDown={startDrag("rotate")} title="Rotate (Shift: 15° steps)"
                    className="absolute left-[calc(50%-7px)] top-[-36px] h-3.5 w-3.5 cursor-grab rounded-full border border-sky-600 bg-white" />
                </div>
              )}
            </div>
            </div>
            <p className="mt-2 text-xs text-slate-500">
              Drag to move; corners scale, edges stretch, the top knob rotates; the wheel zooms. Hold Shift to
              {lockRatio ? " stretch from a corner" : " keep proportions"} or rotate in 15° steps. Blue boxes are where the title page puts its text and logo.
            </p>
          </div>

          <div className="space-y-5 text-sm">
            <div className="flex flex-wrap gap-2">
              <button type="button" className="btn-secondary" disabled={!!busy} onClick={() => fileRef.current?.click()}>
                {state ? "Change picture" : "Choose picture"}
              </button>
              <input ref={fileRef} type="file" accept="image/png,image/jpeg,image/webp" hidden
                onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ""; if (f) void pick(f); }} />
            </div>

            {state && picture && L && (
              <>
                <div>
                  <span className="mb-2 block text-slate-600">Picture</span>
                  <div className="flex flex-wrap gap-2">
                    <button type="button" className="btn-secondary" onClick={() => set({ layer: fitLayer(picture, false) })}>Fit</button>
                    <button type="button" className="btn-secondary" onClick={() => set({ layer: fitLayer(picture, true) })}>Fill</button>
                    <button type="button" className="btn-secondary" onClick={() => set({ layer: { ...L, cx: SLIDE_W / 2, cy: SLIDE_H / 2 } })}>Center</button>
                    <button type="button" className="btn-secondary" disabled={!L.rot} onClick={() => set({ layer: { ...L, rot: 0 } })}>Straighten</button>
                    <button type="button" className="btn-secondary" title="Undo stretching"
                      disabled={Math.abs(L.w / L.h - picture.naturalWidth / picture.naturalHeight) < 0.005}
                      onClick={() => set({ layer: { ...L, h: (L.w * picture.naturalHeight) / picture.naturalWidth } })}>
                      Unstretch
                    </button>
                  </div>
                  <button type="button" className="mt-2 inline-flex items-center gap-1.5 text-slate-600 hover:text-slate-900"
                    aria-pressed={lockRatio} onClick={() => setLockRatio(!lockRatio)}>
                    {lockRatio ? <Lock size={14} /> : <Unlock size={14} />}
                    Corners {lockRatio ? "keep proportions" : "stretch freely"}
                  </button>
                  <div className="mt-2 flex items-center gap-2">
                    <label htmlFor="bg-rot" className="w-16 text-slate-600">Rotation</label>
                    <input id="bg-rot" type="range" min={-180} max={180} value={Math.round(L.rot)} className="flex-1"
                      onChange={(e) => set({ layer: { ...L, rot: Number(e.target.value) } })} />
                    <span className="w-10 text-right tabular-nums">{Math.round(L.rot)}°</span>
                  </div>
                </div>

                <div>
                  <span className="mb-2 block text-slate-600">Behind the picture</span>
                  <Segmented<FillMode> label="Behind the picture" value={state.fill.mode}
                    onChange={(mode) => set({ fill: { ...state.fill, mode } })}
                    options={[
                      { value: "blur", label: "Blurred" },
                      { value: "color", label: "Colour" },
                      { value: "template", label: "Template" },
                    ]} />
                  {state.fill.mode === "color" && (
                    <div className="mt-2 flex items-center gap-2">
                      <input type="color" aria-label="Fill colour" value={state.fill.color}
                        onChange={(e) => set({ fill: { ...state.fill, color: e.target.value } })} />
                      <button type="button" className="text-[var(--color-primary)] hover:underline"
                        onClick={() => set({ fill: { ...state.fill, color: props.mainColor } })}>Use main colour</button>
                      <button type="button" className="text-[var(--color-primary)] hover:underline"
                        onClick={() => set({ fill: { ...state.fill, color: "#ffffff" } })}>White</button>
                    </div>
                  )}
                </div>

                <div>
                  <span className="mb-2 block text-slate-600">Tint (keeps the title readable)</span>
                  <div className="flex items-center gap-2">
                    <input type="color" aria-label="Tint colour" value={state.overlay.color}
                      onChange={(e) => set({ overlay: { ...state.overlay, color: e.target.value } })} />
                    <input type="range" min={0} max={85} value={Math.round(state.overlay.opacity * 100)} className="flex-1"
                      aria-label="Tint strength"
                      onChange={(e) => set({ overlay: { ...state.overlay, opacity: Number(e.target.value) / 100 } })} />
                    <span className="w-10 text-right tabular-nums">{Math.round(state.overlay.opacity * 100)}%</span>
                  </div>
                </div>

                <label className="flex items-center gap-2 text-slate-600">
                  <input type="checkbox" checked={guides} onChange={(e) => setGuides(e.target.checked)} />
                  Show where the title text goes
                </label>
              </>
            )}

            {busy && <p className="text-slate-500">{busy}</p>}
            {error && <p className="text-red-600">{error}</p>}

            <div className="flex gap-2 border-t border-[var(--color-border)] pt-4">
              <button type="button" className="btn-primary" disabled={!state || !picture || !!busy} onClick={() => void apply()}>
                Use this background
              </button>
              <button type="button" className="btn-secondary" onClick={props.onClose}>Cancel</button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
