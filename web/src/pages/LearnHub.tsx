import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ClipboardCheck, NotebookText } from "lucide-react";
import { listGenerations } from "../api";

// Learn's front page: study notes or a practice exam.
export default function LearnHub() {
  const [counts, setCounts] = useState<{ notes: number; exam: number } | null>(null);

  useEffect(() => {
    listGenerations().then((gens) => setCounts({
      notes: gens.filter((g) => g.kind === "notes").length,
      exam: gens.filter((g) => g.kind === "exam").length,
    }), () => undefined);
  }, []);

  const card = "group flex flex-col gap-3 border border-[var(--color-border)] bg-white p-7 no-underline shadow-sm transition hover:-translate-y-0.5 hover:border-[var(--color-primary)] hover:shadow-md";
  return (
    <div className="space-y-8">
      <h1 className="text-3xl">Learn</h1>
      <div className="grid gap-6 md:grid-cols-2">
        <Link to="/learn/notes" className={card}>
          <NotebookText className="h-8 w-8 text-[var(--color-primary)]" />
          <h2 className="text-2xl text-[var(--color-heading)]">Study notes</h2>
          <p className="text-sm text-slate-600">
            Read a paper or lecture with notes made from it: the source and the notes side by side, linked page to
            page, with an AI to ask and progress to tick off.
          </p>
          <span className="mt-auto text-xs text-slate-400">
            {counts ? `${counts.notes} ${counts.notes === 1 ? "notebook" : "notebooks"}` : " "}
          </span>
        </Link>
        <Link to="/learn/exams" className={card}>
          <ClipboardCheck className="h-8 w-8 text-emerald-600" />
          <h2 className="text-2xl text-[var(--color-heading)]">Exams</h2>
          <p className="text-sm text-slate-600">
            Prepare for an exam: from your material and its past papers (or your description of it), mock papers
            as close to the real thing as can be. Print them with their answer keys, or answer them here one
            question at a time.
          </p>
          <span className="mt-auto text-xs text-slate-400">
            {counts ? `${counts.exam} ${counts.exam === 1 ? "exam" : "exams"}` : " "}
          </span>
        </Link>
      </div>
    </div>
  );
}
