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
import { installedFonts } from "./fontDetect";

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
  images?: { file: File; note: string }[];
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
  // One note per image, in the same order (empty when the user wrote none)
  for (const img of input.images ?? []) {
    form.append("images", img.file);
    form.append("image_notes", img.note);
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

// Changes queued on the page, applied in one job that compiles once.
export type QueuedText = { line: number; source: string; start: number; end: number; text: string; raw: boolean };
// `figures` are files returned by addFigures, attached to that request.
export type QueuedPage = { index: number; instruction: string; figures: string[] };
export type QueuedDeck = { instruction: string; figures: string[] };

export const applyChanges = (id: string, texts: QueuedText[], pages: QueuedPage[], deck: QueuedDeck | null) =>
  requestJson<void>(`/api/generations/${id}/batch`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ texts, pages, deck }),
  });

// Images for queued requests join the deck's image files right away.
export type DeckFigure = { file: string; name: string; note: string; width: number; height: number };
export function addFigures(id: string, images: Blob[], names: string[]): Promise<{ figures: DeckFigure[] }> {
  const form = new FormData();
  images.forEach((image, i) => form.append("images", image, names[i]));
  return requestJson(`/api/generations/${id}/figures`, { method: "POST", body: form });
}
export const figureUrl = (id: string, file: string) =>
  `/api/generations/${id}/figures/${encodeURIComponent(file.split("/").pop() ?? file)}`;

// What the model remembers about the user (habits, mistakes to avoid).
export type Memory = { habits: string[]; mistakes: string[] };
export const getMemory = () => requestJson<Memory>("/api/memory");
export const saveMemory = (memory: Memory) =>
  requestJson<Memory>("/api/memory", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(memory),
  });

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
// Learn: the source PDFs, and where each \\src-marked heading of the notes sits.
export const sourcePdfUrl = (id: string, index: number) => `/api/generations/${id}/sources/${index}.pdf`;
export const sourceTextUrl = (id: string) => `/api/generations/${id}/source.txt`;
export type SourceAnchor = {
  doc: number; page: number; to: number; level: number; title: string; notes_page: number; y: number;
};
export type SourceMap = { sources: { index: number; name: string; pages: number }[]; anchors: SourceAnchor[] };
export const getSourceMap = (id: string) => requestJson<SourceMap>(`/api/generations/${id}/source-map`);
// Learn: questions to the model, kept as threads (see server/app/chat.py).
export type QuoteBox = { x: number; y: number; w: number; h: number };
export type ChatAnchor = {
  where: "source" | "notes"; doc: number; page: number; text: string; rects: QuoteBox[];
  msg?: string; gone?: boolean;
  area?: boolean; image?: string; // a dragged box, sent to the model as a crop
};
export type ChatMessage = {
  id: string; role: "user" | "assistant"; content: string; status: "pending" | "done" | "error"; at: number;
};
// session: the page of the conversation list it is on; its marks show in every session.
export type ChatThread = { id: string; number: number; session: number; anchors: ChatAnchor[]; messages: ChatMessage[]; created_at: number };
export const listThreads = (id: string) =>
  requestJson<{ threads: ChatThread[] }>(`/api/generations/${id}/threads`).then((r) => r.threads);
export const askQuestion = (id: string, question: string, thread: string | null, anchors: ChatAnchor[], session: number) =>
  requestJson<ChatThread>(`/api/generations/${id}/threads`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, thread, anchors, session }),
  });
export const threadImageUrl = (id: string, name: string) => `/api/generations/${id}/threads/images/${name}`;
export const deleteThread = (id: string, thread: string) =>
  requestJson<void>(`/api/generations/${id}/threads/${thread}`, { method: "DELETE" });
// Learn: the parts of the notes marked as learnt (keys from the heading titles).
export const getProgress = (id: string) =>
  requestJson<{ done: string[] }>(`/api/generations/${id}/progress`).then((r) => r.done);
export const saveProgress = (id: string, done: string[]) =>
  requestJson<{ done: string[] }>(`/api/generations/${id}/progress`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ done }),
  });

export const texUrl = (id: string) => `/api/generations/${id}/tex`;
export const bundleUrl = (id: string) => `/api/generations/${id}/bundle.zip`;
export const pptxUrl = (id: string, fonts?: string[]) =>
  `/api/generations/${id}/deck.pptx${fonts ? `?fonts=${encodeURIComponent(fonts.join(","))}` : ""}`;

// The fonts installed here, among those the PPTX exporter knows: every PDF font
// is written as the nearest of them (same design, else the same style).
let installedCache: Promise<string[]> | null = null;
export function localFonts(): Promise<string[]> {
  installedCache ??= requestJson<string[]>("/api/fonts").then(installedFonts).catch(() => {
    installedCache = null;
    return [];
  });
  return installedCache;
}
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
export const getAgyModels = () =>
  requestJson<{ models: { value: string; label: string }[] }>("/api/model/agy/models");
export const startAgyLogin = () =>
  requestJson<ModelStatus & { url: string }>("/api/auth/agy/login", { method: "POST" });
export const submitAgyCode = (code: string) =>
  requestJson<ModelStatus>("/api/auth/agy/code", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code }),
  });
export const cancelAgyLogin = () =>
  requestJson<ModelStatus>("/api/auth/agy/login", { method: "DELETE" });
export const signOutAgy = () => requestJson<ModelStatus>("/api/auth/agy", { method: "DELETE" });
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

// Learn: exams and their mock papers (see server/app/exam.py).
export type QuestionType = "single" | "multiple" | "truefalse" | "fill" | "short" | "calc" | "other";
export type SpecRow = {
  type: QuestionType; count: number; points: number;
  items?: { points: number; parts: string }[]; // short/calc/other: each question's points and split
  description?: string; // other: what the questions look like
};
export type ExamSpec = { types: SpecRow[]; description: string; duration: number; extra: string };
export type BlueprintSection = {
  title: string; type: QuestionType; count: number; points: number[]; parts: string[]; format: string; passage: boolean;
};
export type Topic = { name: string; weight: number; note: string };
// What the exam is like: structure, topics, wording, marking.
export type Profile = {
  title: string; instructions: string; duration: number; style: string; marking: string;
  sections: BlueprintSection[]; topics: Topic[];
};
export type ExamPart = { stem: string; answer: string; rubric: string[]; points: number };
export type ExamQuestion = {
  id: string; number: number; type: QuestionType; mode: "single" | "multiple" | "truefalse" | "fill" | "open";
  stem: string; points: number; explanation: string; topic?: string; options?: string[];
  answer: string | string[] | boolean | string[][]; rubric?: string[]; parts?: ExamPart[];
};
export type ExamSection = { title: string; type: QuestionType; instructions: string; passage: string; questions: ExamQuestion[] };
export type Attempt = { score: number; total: number; answered: number; correct: number; at: number };
export type Drill = { topic: string; type: QuestionType; count: number };
export type PaperSummary = {
  id: string; title: string; total: number; questions: number; created: number; attempts: Attempt[]; focus: string;
  drill: Drill | null; // set for drills: one type of question on one topic
};
// A topic's mastery: share of points earned, recent answers counting most; null before any answer.
export type Mastery = { name: string; weight: number; answered: number; mastery: number | null; recent: number[];
  own?: OwnTopic }; // set for a topic the student added
// A topic the student added: what it covers and example questions (pasted, or files).
export type OwnTopic = { id: string; name: string; note: string; text: string; files: string[] };
export type Exam = PaperSummary & { instructions: string; duration: number; sections: ExamSection[] };
export type ExamState = {
  spec: ExamSpec | null; profile: Profile | null; papers: PaperSummary[];
  stage: "drafting" | "profile" | "ready"; past: string[]; mastery: Mastery[];
};
export type Grade = { id: string; qid: string; part: number | null; status: "pending" | "done" | "error"; score: number; max: number; feedback: string };

const exam = (id: string) => `/api/generations/${id}/exam`;
const json = (method: string, body: unknown): RequestInit =>
  ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

export function createExam(input: {
  title: string; language: SlideLanguage; spec: ExamSpec; files?: File[]; source_text?: string; past: File[];
}): Promise<{ id: string }> {
  const form = new FormData();
  form.append("title", input.title);
  form.append("language", input.language);
  form.append("spec", JSON.stringify(input.spec));
  if (input.source_text) form.append("source_text", input.source_text);
  for (const f of input.files ?? []) form.append("files", f);
  for (const f of input.past) form.append("past", f);
  return requestJson<{ id: string }>("/api/exams", { method: "POST", body: form });
}
export const getExam = (id: string) => requestJson<ExamState>(exam(id));
export const saveProfile = (id: string, profile: Profile) => requestJson<Profile>(`${exam(id)}/profile`, json("PUT", { profile }));
export const redraftProfile = (id: string) => requestJson<void>(`${exam(id)}/redraft`, { method: "POST" });
export function addPastPapers(id: string, files: File[]): Promise<void> {
  const form = new FormData();
  for (const f of files) form.append("past", f);
  return requestJson<void>(`${exam(id)}/past`, { method: "POST", body: form });
}
export const newPaper = (id: string, focus: string) => requestJson<void>(`${exam(id)}/papers`, json("POST", { focus }));
export const newDrill = (id: string, drill: Drill) => requestJson<void>(`${exam(id)}/drills`, json("POST", drill));
export function saveOwnTopic(id: string, tid: string | null,
  topic: { name: string; note: string; text: string; files: File[] }): Promise<OwnTopic> {
  const form = new FormData();
  form.append("name", topic.name);
  form.append("note", topic.note);
  form.append("text", topic.text);
  for (const f of topic.files) form.append("files", f);
  return requestJson<OwnTopic>(`${exam(id)}/topics${tid ? `/${tid}` : ""}`, { method: tid ? "PUT" : "POST", body: form });
}
export const deleteOwnTopic = (id: string, tid: string) => requestJson<void>(`${exam(id)}/topics/${tid}`, { method: "DELETE" });
export const deleteOwnTopicFile = (id: string, tid: string, name: string) =>
  requestJson<void>(`${exam(id)}/topics/${tid}/files/${name}`, { method: "DELETE" });
export const ownTopicFileUrl = (id: string, tid: string, name: string) => `${exam(id)}/topics/${tid}/files/${name}`;
export const saveResult = (id: string, pid: string, qid: string, score: number) =>
  requestJson<unknown>(`${exam(id)}/papers/${pid}/results`, json("POST", { qid, score }));
export const getPaper = (id: string, pid: string) => requestJson<Exam>(`${exam(id)}/papers/${pid}`);
export const deletePaper = (id: string, pid: string) => requestJson<void>(`${exam(id)}/papers/${pid}`, { method: "DELETE" });
export const examPdfUrl = (id: string, pid: string, name: "paper" | "answers", download = false) =>
  `${exam(id)}/papers/${pid}/${name}.pdf${download ? "?download=true" : ""}`;
export function gradeAnswer(id: string, pid: string, qid: string, part: number | null, answer: string, image?: File): Promise<Grade> {
  const form = new FormData();
  form.append("qid", qid);
  form.append("part", String(part ?? -1));
  form.append("answer", answer);
  if (image) form.append("image", image);
  return requestJson<Grade>(`${exam(id)}/papers/${pid}/grade`, { method: "POST", body: form });
}
export const getGrade = (id: string, grade: string) => requestJson<Grade>(`${exam(id)}/grade/${grade}`);
export const saveAttempt = (id: string, pid: string, a: Omit<Attempt, "at">) =>
  requestJson<Attempt[]>(`${exam(id)}/papers/${pid}/attempts`, json("POST", a));
