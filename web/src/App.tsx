import { Link, Navigate, Route, Routes, useParams } from "react-router-dom";
import Home from "./pages/Home";
import Generation from "./pages/Generation";
import ModelMenu from "./components/ModelMenu";
import { AuthGate, useAuth } from "./auth";
import { ModelProvider } from "./model";

// Key the page by generation id so navigating /g/a → /g/b (e.g. after
// "Regenerate") remounts with fresh state instead of reusing stale
// frames/progress from the previous generation.
function GenerationRoute() {
  const { id } = useParams();
  return <Generation key={id} />;
}

function Account() {
  const { user, signOut } = useAuth();
  if (!user) return null;
  return (
    <span className="flex items-center gap-2 text-sm text-slate-500">
      {user}
      <button type="button" className="text-[var(--color-primary)] hover:underline" onClick={() => void signOut()}>
        Sign out
      </button>
    </span>
  );
}

export default function App() {
  return (
    <AuthGate>
    <ModelProvider>
    <div className="min-h-screen">
      <header className="border-b border-[var(--color-border)] bg-[var(--color-surface-container)]">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-4 px-6 py-4">
          <Link to="/" className="text-xl tracking-wide text-[var(--color-heading)] no-underline">
            TeXDeck
          </Link>
          <div className="flex items-center gap-4">
            <ModelMenu />
            <Account />
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-6 py-8">
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/g/:id" element={<GenerationRoute />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
    </ModelProvider>
    </AuthGate>
  );
}
