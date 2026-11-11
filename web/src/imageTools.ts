// Image work for the template editor, all in the browser: the server only
// checks and stores the PNGs this produces, so what the editor shows is
// exactly what the deck gets.

export const SLIDE_W = 1920;
export const SLIDE_H = 1080;
const MAX_SOURCE_SIDE = 3000; // originals kept for re-editing
const MAX_LOGO_SIDE = 1024;

export function loadImage(src: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error("The image could not be loaded"));
    img.src = src;
  });
}

function canvas(w: number, h: number): [HTMLCanvasElement, CanvasRenderingContext2D] {
  const c = document.createElement("canvas");
  c.width = w;
  c.height = h;
  const ctx = c.getContext("2d");
  if (!ctx) throw new Error("This browser cannot edit images");
  return [c, ctx];
}

export function toBlob(c: HTMLCanvasElement, type = "image/png", quality?: number): Promise<Blob> {
  return new Promise((resolve, reject) =>
    c.toBlob((b) => (b ? resolve(b) : reject(new Error("The image could not be encoded"))), type, quality),
  );
}

// A picked file, upright (browsers apply EXIF orientation when decoding),
// at most MAX_SOURCE_SIDE on a side. PNGs stay PNG so transparency survives.
export async function prepareSource(file: File): Promise<Blob> {
  if (!/^image\/(png|jpeg|webp|gif|bmp)$/.test(file.type)) throw new Error("Use a PNG or JPEG image");
  const bitmap = await createImageBitmap(file);
  const scale = Math.min(1, MAX_SOURCE_SIDE / Math.max(bitmap.width, bitmap.height));
  const [c, ctx] = canvas(Math.round(bitmap.width * scale), Math.round(bitmap.height * scale));
  ctx.drawImage(bitmap, 0, 0, c.width, c.height);
  bitmap.close();
  return file.type === "image/jpeg" ? toBlob(c, "image/jpeg", 0.92) : toBlob(c, "image/png");
}

// ---- images placed on the slides ----

const MAX_FIGURE_SIDE = 2400;

// Any image this browser can decode (WebP, GIF, HEIC on Safari...), upright,
// at most MAX_FIGURE_SIDE on a side, as the PNG or JPEG the server accepts.
// Photos stay JPEG; everything else becomes PNG so screenshots stay sharp.
export async function prepareFigure(file: File): Promise<File> {
  let bitmap: ImageBitmap;
  try {
    bitmap = await createImageBitmap(file);
  } catch {
    throw new Error(`${file.name}: this browser cannot read the image; convert it to PNG or JPEG`);
  }
  const scale = Math.min(1, MAX_FIGURE_SIDE / Math.max(bitmap.width, bitmap.height));
  const [c, ctx] = canvas(Math.round(bitmap.width * scale), Math.round(bitmap.height * scale));
  ctx.drawImage(bitmap, 0, 0, c.width, c.height);
  bitmap.close();
  const photo = /^image\/(jpeg|heic|heif)$/.test(file.type) || /\.(jpe?g|heic|heif)$/i.test(file.name);
  const blob = photo ? await toBlob(c, "image/jpeg", 0.92) : await toBlob(c, "image/png");
  const base = file.name.replace(/\.[^.]+$/, "") || "image";
  return new File([blob], `${base}.${photo ? "jpg" : "png"}`, { type: blob.type });
}

// ---- logos ----

// "outside" clears the border colour reached from the border (a white letter
// on a coloured badge stays); "all" clears it everywhere (the holes in
// letters too). Which one is right depends on the logo, so the user picks.
export type LogoBackground = "keep" | "outside" | "all";

// Background = the colour along the image border. Its margins are always
// trimmed so the logo fills its slot in the template.
export async function processLogo(src: string, background: LogoBackground): Promise<Blob> {
  const img = await loadImage(src);
  const scale = Math.min(1, MAX_LOGO_SIDE / Math.max(img.naturalWidth, img.naturalHeight));
  const w = Math.max(1, Math.round(img.naturalWidth * scale));
  const h = Math.max(1, Math.round(img.naturalHeight * scale));
  const [c, ctx] = canvas(w, h);
  ctx.drawImage(img, 0, 0, w, h);
  const data = ctx.getImageData(0, 0, w, h);
  const px = data.data;

  const border = borderColour(px, w, h);
  const TOL = 40; // max channel difference that still counts as background
  const diff = (i: number) => {
    if (border.transparent) return px[i + 3] < 16 ? 0 : 255;
    if (px[i + 3] < 16) return 0;
    return Math.max(Math.abs(px[i] - border.r), Math.abs(px[i + 1] - border.g), Math.abs(px[i + 2] - border.b));
  };

  // Flood the background in from the border.
  const bg = new Uint8Array(w * h);
  const stack: number[] = [];
  const seed = (x: number, y: number) => {
    const p = y * w + x;
    if (!bg[p] && diff(p * 4) <= TOL) {
      bg[p] = 1;
      stack.push(p);
    }
  };
  for (let x = 0; x < w; x++) { seed(x, 0); seed(x, h - 1); }
  for (let y = 0; y < h; y++) { seed(0, y); seed(w - 1, y); }
  while (stack.length) {
    const p = stack.pop()!;
    const x = p % w;
    const y = (p - x) / w;
    if (x > 0) seed(x - 1, y);
    if (x < w - 1) seed(x + 1, y);
    if (y > 0) seed(x, y - 1);
    if (y < h - 1) seed(x, y + 1);
  }

  if (background !== "keep" && !border.transparent) {
    const clear = bg.slice();
    if (background === "all") for (let p = 0; p < w * h; p++) if (diff(p * 4) <= TOL) clear[p] = 1;
    for (let p = 0; p < w * h; p++) {
      const i = p * 4;
      if (clear[p]) {
        px[i + 3] = 0;
        continue;
      }
      // Anti-aliased edge next to the removed area: fade by closeness to it.
      const x = p % w;
      const touches = (x > 0 && clear[p - 1]) || (x < w - 1 && clear[p + 1]) || (p >= w && clear[p - w]) || (p < w * (h - 1) && clear[p + w]);
      const d = diff(i);
      if (touches && d < 2 * TOL) px[i + 3] = Math.round((px[i + 3] * (d - TOL)) / TOL);
    }
    ctx.putImageData(data, 0, 0);
  }

  // Trim the background margins.
  let x0 = w, y0 = h, x1 = -1, y1 = -1;
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      if (!bg[y * w + x]) {
        if (x < x0) x0 = x;
        if (x > x1) x1 = x;
        if (y < y0) y0 = y;
        if (y > y1) y1 = y;
      }
    }
  }
  if (x1 < 0) throw new Error("The logo is empty once its background is removed");
  const [out, octx] = canvas(x1 - x0 + 1, y1 - y0 + 1);
  octx.drawImage(c, x0, y0, out.width, out.height, 0, 0, out.width, out.height);
  return toBlob(out);
}

function borderColour(px: Uint8ClampedArray, w: number, h: number) {
  // The most common of the four corners decides; a transparent corner wins ties.
  const corners = [0, w - 1, (h - 1) * w, h * w - 1].map((p) => p * 4);
  const transparent = corners.filter((i) => px[i + 3] < 16).length >= 2;
  const i = corners.find((c) => corners.filter((d) => Math.abs(px[c] - px[d]) + Math.abs(px[c + 1] - px[d + 1]) + Math.abs(px[c + 2] - px[d + 2]) < 30).length >= 2) ?? corners[0];
  return { transparent, r: px[i], g: px[i + 1], b: px[i + 2] };
}

// ---- background ----

export type FillMode = "color" | "template" | "blur";

export type BackgroundState = {
  source: string; // asset id of the uploaded picture
  layer: { cx: number; cy: number; w: number; h: number; rot: number }; // slide pixels, degrees
  fill: { mode: FillMode; color: string };
  overlay: { color: string; opacity: number }; // 0..1
};

export function fitLayer(img: { naturalWidth: number; naturalHeight: number }, cover: boolean) {
  const s = (cover ? Math.max : Math.min)(SLIDE_W / img.naturalWidth, SLIDE_H / img.naturalHeight);
  return { cx: SLIDE_W / 2, cy: SLIDE_H / 2, w: img.naturalWidth * s, h: img.naturalHeight * s, rot: 0 };
}

// One drawing routine for the on-screen editor and the exported PNG.
export function drawBackground(
  ctx: CanvasRenderingContext2D,
  state: BackgroundState,
  picture: HTMLImageElement,
  templateBackground: HTMLImageElement | null,
) {
  ctx.save();
  ctx.clearRect(0, 0, SLIDE_W, SLIDE_H);
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = "high";
  const { fill, layer, overlay } = state;
  if (fill.mode === "template" && templateBackground) {
    ctx.drawImage(templateBackground, 0, 0, SLIDE_W, SLIDE_H);
  } else if (fill.mode === "blur") {
    // Shrink then enlarge: a soft blur that looks the same in every browser.
    const [small, sctx] = canvas(48, 27);
    const cover = Math.max(48 / picture.naturalWidth, 27 / picture.naturalHeight);
    const sw = picture.naturalWidth * cover;
    const sh = picture.naturalHeight * cover;
    sctx.imageSmoothingQuality = "high";
    sctx.drawImage(picture, (48 - sw) / 2, (27 - sh) / 2, sw, sh);
    ctx.drawImage(small, 0, 0, SLIDE_W, SLIDE_H);
  } else {
    ctx.fillStyle = fill.color;
    ctx.fillRect(0, 0, SLIDE_W, SLIDE_H);
  }
  ctx.translate(layer.cx, layer.cy);
  ctx.rotate((layer.rot * Math.PI) / 180);
  ctx.drawImage(picture, -layer.w / 2, -layer.h / 2, layer.w, layer.h);
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  if (overlay.opacity > 0) {
    ctx.globalAlpha = overlay.opacity;
    ctx.fillStyle = overlay.color;
    ctx.fillRect(0, 0, SLIDE_W, SLIDE_H);
  }
  ctx.restore();
}

export async function exportBackground(
  state: BackgroundState,
  picture: HTMLImageElement,
  templateBackground: HTMLImageElement | null,
): Promise<Blob> {
  const [c, ctx] = canvas(SLIDE_W, SLIDE_H);
  drawBackground(ctx, state, picture, templateBackground);
  return toBlob(c);
}
