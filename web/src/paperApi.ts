import { requestJson } from "./api";
import type { GenerationMeta } from "./types";

// Papering: LaTeX projects (generations of kind "paper").

export const ENGINES = ["xelatex", "pdflatex", "lualatex"] as const;
export type Engine = (typeof ENGINES)[number];
export type PaperSettings = { engine: Engine; main: string };
export type PaperEntry = { path: string; directory: boolean; size?: number };
export type PaperBuild = { running: boolean; ok: boolean; error: string; pdf: string; finished_at: number; log?: string };
export type PaperSource = { text: string; revision: string };
export type PaperProject = { meta: GenerationMeta; settings: PaperSettings; entries: PaperEntry[]; build: PaperBuild };

const base = (id: string) => `/api/papers/${id}`;
const q = (path: string) => `path=${encodeURIComponent(path)}`;
const json = (method: string, body: unknown): RequestInit =>
  ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

export function createPaper(title: string, language: "en" | "zh", archive?: File | null) {
  const form = new FormData();
  form.append("title", title);
  form.append("language", language);
  if (archive) form.append("archive", archive);
  return requestJson<{ id: string }>("/api/papers", { method: "POST", body: form });
}
export const getProject = (id: string) => requestJson<PaperProject>(base(id));
export const readSource = (id: string, path: string) => requestJson<PaperSource>(`${base(id)}/source?${q(path)}`);
export const saveSource = (id: string, path: string, text: string, revision: string) =>
  requestJson<{ revision: string }>(`${base(id)}/source?${q(path)}`, json("PUT", { text, revision }));
export const newFile = (id: string, path: string, directory = false) =>
  requestJson<void>(`${base(id)}/files`, json("POST", { path, directory }));
export const deleteFile = (id: string, path: string) =>
  requestJson<void>(`${base(id)}/files?${q(path)}`, { method: "DELETE" });
export const renameFile = (id: string, path: string, to: string) =>
  requestJson<void>(`${base(id)}/rename`, json("POST", { path, to }));
export function uploadFiles(id: string, files: File[], folder: string, paths?: string[]) {
  const form = new FormData();
  form.append("folder", folder);
  files.forEach((f, i) => {
    form.append("files", f);
    if (paths) form.append("paths", paths[i]);
  });
  return requestJson<{ saved: string[]; errors: string[] }>(`${base(id)}/upload`, { method: "POST", body: form });
}
export const saveSettings = (id: string, s: PaperSettings) =>
  requestJson<PaperSettings>(`${base(id)}/settings`, json("PUT", s));
export const compile = (id: string) => requestJson<PaperBuild>(`${base(id)}/compile`, { method: "POST" });
export const getBuild = (id: string) => requestJson<PaperBuild>(`${base(id)}/build`);
export const forwardSync = (id: string, path: string, line: number) =>
  requestJson<{ page: number; x: number; y: number }>(`${base(id)}/forward?${q(path)}&line=${line}`);
export const inverseSync = (id: string, page: number, x: number, y: number) =>
  requestJson<{ path: string; line: number }>(`${base(id)}/inverse?page=${page}&x=${x.toFixed(2)}&y=${y.toFixed(2)}`);
export const pdfUrl = (id: string, version: string) => `${base(id)}/pdf?v=${encodeURIComponent(version)}`;
export const assetUrl = (id: string, path: string) => `${base(id)}/asset?${q(path)}`;
export const zipUrl = (id: string) => `${base(id)}/project.zip`;
export const watchUrl = (id: string) =>
  `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/papers/${id}`;
