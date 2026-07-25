import { Navigate, Route, Routes } from "react-router-dom";
import Home from "./pages/Home";
import Generation from "./pages/Generation";

export default function App() {
  return (
    <div className="min-h-screen">
      <header className="border-b border-[var(--color-border)] bg-[var(--color-surface-container)]">
        <div className="mx-auto flex max-w-6xl items-center justify-between px-6 py-4">
          <a href="/" className="text-xl tracking-wide text-[var(--color-heading)] no-underline">
            TeXDeck
          </a>
          <span className="text-xs text-slate-500">Any PDF → a finished Beamer deck</span>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-6 py-8">
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/g/:id" element={<Generation />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  );
}
