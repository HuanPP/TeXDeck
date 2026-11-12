// Which of the given font names are installed on this computer. A missing
// font falls back to the generic family, so the sample measures exactly as
// wide as the generic family alone; an installed one (almost always) does
// not. Three generic families guard against a font that happens to match one.
const SAMPLE = "mmmmmmmmmmlliWWQ@0123永和宋楷仿黑";
const GENERIC = ["monospace", "serif", "sans-serif"];

export function installedFonts(names: string[]): string[] {
  const ctx = document.createElement("canvas").getContext("2d");
  if (!ctx) return [];
  const width = (font: string) => {
    ctx.font = font;
    return ctx.measureText(SAMPLE).width;
  };
  const base = GENERIC.map((g) => width(`72px ${g}`));
  return names.filter((name) =>
    GENERIC.some((g, i) => width(`72px "${name.replace(/"/g, "")}", ${g}`) !== base[i]),
  );
}
