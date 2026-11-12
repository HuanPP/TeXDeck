import { useEffect } from "react";
import { Link, Navigate, NavLink, Route, Routes, useLocation, useParams } from "react-router-dom";
import Home from "./pages/Home";
import Generation from "./pages/Generation";
import TemplateEditor from "./pages/TemplateEditor";
import ModelMenu from "./components/ModelMenu";
import InvitesMenu from "./components/InvitesMenu";
import MemoryMenu from "./components/MemoryMenu";
import { AuthGate, useAuth } from "./auth";
import { ModelProvider } from "./model";

// Key the page by generation id so navigating /g/a → /g/b (e.g. after
// "Regenerate") remounts with fresh state instead of reusing stale
// frames/progress from the previous generation.
function GenerationRoute() {
  const { id } = useParams();
  return <Generation key={id} />;
}

// A fresh editor per template key, like GenerationRoute.
function TemplateEditorRoute() {
  const { key } = useParams();
  return <TemplateEditor key={key} />;
}

// The two halves of the app; "/" opens whichever was used last.
const SECTIONS = [
  { path: "/learn", label: "Learn" },
  { path: "/present", label: "Presentation" },
] as const;
const LAST_SECTION = "texdeck.section";

function Section({ path, children }: { path: string; children: React.ReactNode }) {
  useEffect(() => localStorage.setItem(LAST_SECTION, path), [path]);
  return <>{children}</>;
}

function RootRedirect() {
  const { search } = useLocation();
  const last = localStorage.getItem(LAST_SECTION);
  const to = SECTIONS.some((s) => s.path === last) && !search.includes("template=") ? last! : "/present";
  return <Navigate to={to + search} replace />;
}

function SectionNav() {
  return (
    <nav className="flex items-center gap-1" aria-label="Sections">
      {SECTIONS.map((s) => (
        <NavLink key={s.path} to={s.path}
          className={({ isActive }) =>
            `px-3 py-1.5 text-sm no-underline ${isActive
              ? "bg-white text-[var(--color-heading)] shadow-sm"
              : "text-slate-500 hover:text-slate-800"}`}>
          {s.label}
        </NavLink>
      ))}
    </nav>
  );
}

function Account() {
  const { user, admin, signOut } = useAuth();
  if (!user) return null;
  return (
    <span className="flex items-center gap-2 text-sm text-slate-500">
      {admin && <InvitesMenu />}
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
          <div className="flex items-center gap-6">
            <Link to="/" className="flex items-center gap-2 text-xl tracking-wide text-[var(--color-heading)] no-underline">
              <img src="/favicon.svg" alt="" className="h-7 w-7" />
              TeXDeck
            </Link>
            <SectionNav />
          </div>
          <div className="flex items-center gap-4">
            <ModelMenu />
            <MemoryMenu />
            <Account />
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-6 py-8">
        <Routes>
          <Route path="/" element={<RootRedirect />} />
          <Route path="/learn" element={<Section path="/learn"><Home key="notes" kind="notes" /></Section>} />
          <Route path="/present" element={<Section path="/present"><Home key="deck" kind="deck" /></Section>} />
          <Route path="/g/:id" element={<GenerationRoute />} />
          <Route path="/templates/new" element={<TemplateEditor />} />
          <Route path="/templates/:key" element={<TemplateEditorRoute />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
    </ModelProvider>
    </AuthGate>
  );
}
