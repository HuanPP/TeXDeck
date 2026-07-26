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
};

export type TemplateItem = { key: string; name: string };
export type FrameItem = { index: number; title: string; page: number; tex: string };
export type TaskEvent = {
  gen_id: string;
  status: TaskStatus;
  percent: number;
  stage: string;
  error: string;
};
