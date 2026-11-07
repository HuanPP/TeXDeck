import type { GenerationMeta } from "./types";

// A blank title is filled from the finished deck; until then, name the
// generation after its first source file.
export function displayTitle(g: GenerationMeta): string {
  if (g.title.trim()) return g.title;
  const first = g.source_names[0];
  return first ? first.replace(/\.pdf$/i, "") : "Untitled deck";
}
