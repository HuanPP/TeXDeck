import type { TaskStatus } from "../types";

const LABELS: Record<TaskStatus, string> = {
  running: "Generating",
  succeeded: "Done",
  failed: "Failed",
  review: "To review",
};
const STYLES: Record<TaskStatus, string> = {
  running: "bg-sky-50 text-sky-700 border-sky-200",
  succeeded: "bg-emerald-50 text-emerald-700 border-emerald-200",
  failed: "bg-red-50 text-red-700 border-red-200",
  review: "bg-amber-50 text-amber-800 border-amber-200",
};

export default function StatusBadge({ status }: { status: TaskStatus }) {
  return (
    <span className={`inline-flex items-center border px-2 py-0.5 text-xs ${STYLES[status]}`}>
      {LABELS[status]}
    </span>
  );
}
