import type { FrameItem, GenerationMeta, ModelStatus, Provider, SlideLanguage, TemplateItem } from "./types";

async function readError(res: Response): Promise<string> {
  const text = await res.text();
  if (!text.trim()) return `Request failed (HTTP ${res.status})`;
  try {
    const data = JSON.parse(text) as { detail?: unknown; message?: string };
    if (typeof data.detail === "string" && data.detail.trim()) return data.detail;
    if (Array.isArray(data.detail)) {
      const parts = data.detail
        .map((item) => {
          if (typeof item === "string") return item;
          if (item && typeof item === "object" && "msg" in item) {
            const msg = (item as { msg: unknown }).msg;
            return typeof msg === "string" ? msg : null;
          }
          return null;
        })
        .filter((x): x is string => Boolean(x?.trim()));
      if (parts.length) return parts.join("; ");
    }
    if (data.message?.trim()) return data.message;
  } catch {
    /* non-JSON: return raw text */
  }
  return text.trim();
}

// A 401 means the session ended (expired, password changed, signed out
// elsewhere); the sign-in gate listens for this and shows the sign-in page.
export const SIGNED_OUT_EVENT = "texdeck:signed-out";

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (res.status === 401 && url !== "/api/auth/session") window.dispatchEvent(new Event(SIGNED_OUT_EVENT));
  if (!res.ok) throw new Error(await readError(res));
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const listTemplates = () => requestJson<TemplateItem[]>("/api/templates");
export const listGenerations = () => requestJson<GenerationMeta[]>("/api/generations");
export const getGeneration = (id: string) =>
  requestJson<GenerationMeta>(`/api/generations/${id}`);
export const deleteGeneration = (id: string) =>
  requestJson<void>(`/api/generations/${id}`, { method: "DELETE" });
export const getFrames = (id: string) =>
  requestJson<FrameItem[]>(`/api/generations/${id}/frames`);

export function createGeneration(input: {
  title: string;
  language: SlideLanguage;
  template_key: string;
  instruction: string;
  author?: string;
  institute?: string;
  files?: File[];
  source_text?: string;
}): Promise<{ id: string }> {
  const form = new FormData();
  form.append("title", input.title);
  form.append("language", input.language);
  form.append("template_key", input.template_key);
  form.append("instruction", input.instruction);
  form.append("author", input.author ?? "");
  form.append("institute", input.institute ?? "");
  if (input.files?.length) {
    // Append order = reading order; the server preserves multipart part order
    for (const f of input.files) form.append("files", f);
  } else if (input.source_text !== undefined) {
    form.append("source_text", input.source_text);
  }
  return requestJson("/api/generations", { method: "POST", body: form });
}

export function editFrame(id: string, index: number, instruction: string): Promise<void> {
  return requestJson(`/api/generations/${id}/frames/${index}/edit`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ instruction }),
  });
}

export const regenerateGeneration = (id: string) =>
  requestJson<{ id: string }>(`/api/generations/${id}/regenerate`, { method: "POST" });

export function refineGeneration(id: string, instruction: string): Promise<void> {
  return requestJson(`/api/generations/${id}/refine`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ instruction }),
  });
}

export const templatePreviewUrl = (key: string) =>
  `/api/templates/${encodeURIComponent(key)}/preview.png`;
export const pdfUrl = (id: string, bust = 0) =>
  `/api/generations/${id}/pdf${bust ? `?t=${bust}` : ""}`;
export const texUrl = (id: string) => `/api/generations/${id}/tex`;
export const bundleUrl = (id: string) => `/api/generations/${id}/bundle.zip`;
export const logUrl = (id: string) => `/api/generations/${id}/log`;

export const getModelStatus = () => requestJson<ModelStatus>("/api/model");
export const startChatGptLogin = () =>
  requestJson<{ url: string }>("/api/auth/chatgpt/login", { method: "POST" });
export const finishChatGptLogin = (url: string) =>
  requestJson<ModelStatus>("/api/auth/chatgpt/callback", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url }),
  });
export const signOutChatGpt = () =>
  requestJson<ModelStatus>("/api/auth/chatgpt", { method: "DELETE" });
export const startClaudeLogin = () =>
  requestJson<ModelStatus & { url: string }>("/api/auth/claude/login", { method: "POST" });
export const submitClaudeCode = (code: string) =>
  requestJson<ModelStatus>("/api/auth/claude/code", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code }),
  });
export const cancelClaudeLogin = () =>
  requestJson<ModelStatus>("/api/auth/claude/login", { method: "DELETE" });
export const selectProvider = (provider: Provider) =>
  requestJson<ModelStatus>("/api/model/provider", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ provider }),
  });
export const saveApiKey = (input: { api_key: string; base_url: string; model: string }) =>
  requestJson<ModelStatus>("/api/model/api", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
export const setProviderModel = (provider: Provider, model: string) =>
  requestJson<ModelStatus>("/api/model/model", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ provider, model }),
  });
export const getChatGptModels = () => requestJson<{ models: string[] }>("/api/model/chatgpt/models");
export const removeApiKey = () => requestJson<ModelStatus>("/api/model/api", { method: "DELETE" });

export type Session = { enabled: boolean; user: string | null };
export const getSession = () => requestJson<Session>("/api/auth/session");
export const signIn = (username: string, password: string) =>
  requestJson<Session>("/api/auth/session", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
export const signOut = () => requestJson<Session>("/api/auth/session", { method: "DELETE" });
