import type {
  CustomTemplateSpec,
  FrameItem,
  GenerationMeta,
  ImageSlot,
  LayoutBox,
  ModelStatus,
  Provider,
  SlideLanguage,
  TemplateBase,
  TemplateDraft,
  TemplateItem,
} from "./types";

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
  // 202/204 from edit/refine/delete carry no body.
  const text = await res.text();
  return (text ? JSON.parse(text) : undefined) as T;
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

// Direct text fixes from the preview: find the source line behind clicked
// text, then replace that line (recompiles without the model).
// `text` is source[start:end]: plain slide text, or LaTeX when `raw` (the
// sentence has math or other commands in it).
export type LocatedText = { line: number; source: string; start: number; end: number; text: string; raw: boolean };

// `before` and `after` are the lines just above and below the clicked one.
export const locateText = (id: string, page: number, click: { text: string; before: string; after: string }) =>
  requestJson<LocatedText>(`/api/generations/${id}/locate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ page, text: click.text, before: click.before, after: click.after }),
  });

// Plain text is escaped and written back around the markup by the server.
export function editSourceText(id: string, hit: LocatedText, value: string): Promise<void> {
  const change = hit.raw
    ? { new: hit.source.slice(0, hit.start) + value + hit.source.slice(hit.end) }
    : { start: hit.start, end: hit.end, text: value };
  return requestJson(`/api/generations/${id}/source-line`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ line: hit.line, old: hit.source, ...change }),
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

export const templatePreviewUrl = (key: string, bust = "") =>
  `/api/templates/${encodeURIComponent(key)}/preview.png${bust ? `?v=${encodeURIComponent(bust)}` : ""}`;
export const templateImageUrl = (key: string, slot: ImageSlot) =>
  `/api/templates/${encodeURIComponent(key)}/images/${slot}`;
export const templateLayout = (key: string) =>
  requestJson<LayoutBox[]>(`/api/templates/${encodeURIComponent(key)}/layout`);

// ---- template editor ----
export const listTemplateBases = () => requestJson<TemplateBase[]>("/api/template-bases");
export const assetUrl = (id: string) => `/api/custom-templates/assets/${id}`;
export function uploadTemplateImage(image: Blob): Promise<{ id: string; width: number; height: number }> {
  const form = new FormData();
  form.append("file", image, image.type === "image/jpeg" ? "image.jpg" : "image.png");
  return requestJson("/api/custom-templates/assets", { method: "POST", body: form });
}
export async function previewTemplateDraft(draft: TemplateDraft, signal?: AbortSignal): Promise<Blob> {
  const res = await fetch("/api/custom-templates/preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(draft),
    signal,
  });
  if (res.status === 401) window.dispatchEvent(new Event(SIGNED_OUT_EVENT));
  if (!res.ok) throw new Error(await readError(res));
  return res.blob();
}
export const getCustomTemplate = (key: string) =>
  requestJson<CustomTemplateSpec>(`/api/custom-templates/${encodeURIComponent(key)}`);
export const createCustomTemplate = (draft: TemplateDraft) =>
  requestJson<CustomTemplateSpec>("/api/custom-templates", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(draft),
  });
export const updateCustomTemplate = (key: string, draft: TemplateDraft) =>
  requestJson<CustomTemplateSpec>(`/api/custom-templates/${encodeURIComponent(key)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(draft),
  });
export const deleteCustomTemplate = (key: string) =>
  requestJson<void>(`/api/custom-templates/${encodeURIComponent(key)}`, { method: "DELETE" });
export const pdfUrl = (id: string, bust = 0) =>
  `/api/generations/${id}/pdf${bust ? `?t=${bust}` : ""}`;
export const texUrl = (id: string) => `/api/generations/${id}/tex`;
export const bundleUrl = (id: string) => `/api/generations/${id}/bundle.zip`;
export const pptxUrl = (id: string) => `/api/generations/${id}/deck.pptx`;
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
export const getChatGptModels = () =>
  requestJson<{ models: { value: string; label: string }[] }>("/api/model/chatgpt/models");
export const removeApiKey = () => requestJson<ModelStatus>("/api/model/api", { method: "DELETE" });

// `registration`: sign-up with an invite code is open; `admin` hands out codes.
export type Session = { enabled: boolean; user: string | null; admin: boolean; registration: boolean };
export const getSession = () => requestJson<Session>("/api/auth/session");
export const register = (invite: string, username: string, password: string) =>
  requestJson<Session>("/api/auth/register", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ invite, username, password }),
  });

export type Invite = { code: string; created_by: string; created_at: string; used_by: string; used_at: string };
export const listInvites = () => requestJson<Invite[]>("/api/auth/invites");
export const createInvite = () => requestJson<Invite[]>("/api/auth/invites", { method: "POST" });
export const revokeInvite = (code: string) =>
  requestJson<Invite[]>(`/api/auth/invites/${encodeURIComponent(code)}`, { method: "DELETE" });
export const signIn = (username: string, password: string) =>
  requestJson<Session>("/api/auth/session", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
export const signOut = () => requestJson<Session>("/api/auth/session", { method: "DELETE" });
