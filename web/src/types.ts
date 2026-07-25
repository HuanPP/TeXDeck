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
  source_name: string;
  status: TaskStatus;
  error: string;
  created_at: string;
  updated_at: string;
  page_count: number;
  frame_count: number;
  edit_count: number;
};

export type TemplateItem = { key: string; name: string };
export type FrameItem = { index: number; title: string; tex: string };
export type TaskEvent = {
  gen_id: string;
  status: TaskStatus;
  percent: number;
  stage: string;
  error: string;
};
