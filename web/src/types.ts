export type TaskStatus = "running" | "succeeded" | "failed";

export type GenerationMeta = {
  id: string;
  title: string;
  author: string;
  institute: string;
  template_key: string;
  template_name: string;
  instruction: string;
  source_kind: string;
  source_names: string[];
  status: TaskStatus;
  error: string;
  created_at: string;
  updated_at: string;
  page_count: number;
  frame_count: number;
  edit_count: number;
  // Optional-defaulted server-side; keep optional for older cached payloads.
  refine_count?: number;
  llm_model?: string;
  llm_prompt_tokens?: number;
  llm_completion_tokens?: number;
  llm_calls?: number;
  language?: SlideLanguage;
  error_kind?: string; // "model" when error came from the model provider
  figures?: FigureItem[]; // user images placed on the slides
  // Pages of the compiled PDF (frame_count counts \begin{frame} only, so it
  // misses the title, section and Q&A pages the template adds). 0 if unknown.
  pdf_pages?: number;
};

export type FigureItem = {
  file: string; // "figures/fig-01.png"
  name: string;
  note: string;
  width: number;
  height: number;
};

export type SlideLanguage = "source" | "en" | "zh";

export type TemplateItem = {
  key: string;
  name: string;
  customizable?: boolean; // built-in pack the template editor can derive from
  custom?: boolean; // made in the template editor
  base?: string;
};

export type ImageSlot = "background" | "logo" | "trans_logo";

export type TemplateBase = {
  key: string;
  name: string;
  colors: { name: string; label: string; default: string }[];
  images: Partial<Record<ImageSlot, string>>; // slot -> label
};

// What the editor sends: unchanged colours and images may be left out.
export type TemplateDraft = {
  base: string;
  name: string;
  colors: Record<string, string>;
  images: Partial<Record<ImageSlot, string | null>>; // asset ids
  editor: Record<string, unknown>;
};

export type CustomTemplateSpec = TemplateDraft & { key: string; created_at: string; updated_at: string };

export type LayoutBox = { kind: "text" | "image"; x: number; y: number; w: number; h: number };
export type FrameItem = { index: number; title: string; page: number; tex: string; notes: string };
export type TaskEvent = {
  gen_id: string;
  status: TaskStatus;
  percent: number;
  stage: string;
  error: string;
};

export type Provider = "chatgpt" | "claude-code" | "agy" | "api";

export type ModelStatus = {
  provider: Provider;
  model: string;
  locked: boolean; // TEXDECK_LLM_PROVIDER pins the provider
  chatgpt: { signed_in: boolean; email: string; pending: boolean; model: string };
  claude: {
    available: boolean;
    signed_in: boolean;
    email: string;
    subscription: string;
    pending: boolean;
    login_error: string;
    model: string;
    models: { value: string; label: string }[];
  };
  agy: {
    available: boolean;
    unavailable_reason: string;
    signed_in: boolean;
    email: string;
    pending: boolean;
    login_error: string;
    model: string;
  };
  api: { configured: boolean; base_url: string; model: string; key_hint: string; saved: boolean };
};
