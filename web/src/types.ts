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
};

export type SlideLanguage = "source" | "en" | "zh";

export type TemplateItem = { key: string; name: string };
export type FrameItem = { index: number; title: string; page: number; tex: string; notes: string };
export type TaskEvent = {
  gen_id: string;
  status: TaskStatus;
  percent: number;
  stage: string;
  error: string;
};

export type Provider = "chatgpt" | "claude-code" | "api";

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
  api: { configured: boolean; base_url: string; model: string; key_hint: string; saved: boolean };
};
