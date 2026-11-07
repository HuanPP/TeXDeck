import { createContext, useContext, useEffect, useState, type FormEvent, type ReactNode } from "react";
import { getSession, SIGNED_OUT_EVENT, signIn, signOut, type Session } from "./api";

// Sign-in gate. A TeXDeck without accounts (local, desktop) passes straight
// through; one with accounts shows only the sign-in form until it succeeds.
type AuthValue = { user: string | null; signOut: () => Promise<void> };
const AuthContext = createContext<AuthValue>({ user: null, signOut: async () => undefined });

export const useAuth = () => useContext(AuthContext);

function SignInForm({ onDone }: { onDone: (session: Session) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      onDone(await signIn(username, password));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setPassword("");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-6">
      <form onSubmit={submit} className="app-surface w-full max-w-sm space-y-4 p-6">
        <h1 className="text-2xl">TeXDeck</h1>
        <div>
          <label className="mb-2 block text-sm text-slate-600" htmlFor="username">Username</label>
          <input id="username" className="input-field" autoComplete="username" autoFocus
            value={username} onChange={(e) => setUsername(e.target.value)} />
        </div>
        <div>
          <label className="mb-2 block text-sm text-slate-600" htmlFor="password">Password</label>
          <input id="password" type="password" className="input-field" autoComplete="current-password"
            value={password} onChange={(e) => setPassword(e.target.value)} />
        </div>
        {error && (
          <div role="alert" className="border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>
        )}
        <button type="submit" className="btn-primary w-full" disabled={busy || !username.trim() || !password}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}

export function AuthGate({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getSession().then(setSession).catch((e) => setError(e instanceof Error ? e.message : String(e)));
    const onSignedOut = () => setSession((s) => (s ? { ...s, user: null } : s));
    window.addEventListener(SIGNED_OUT_EVENT, onSignedOut);
    return () => window.removeEventListener(SIGNED_OUT_EVENT, onSignedOut);
  }, []);

  if (error) return <p className="p-6 text-sm text-red-600">{error}</p>;
  if (!session) return null;
  if (session.enabled && !session.user) return <SignInForm onDone={setSession} />;

  const value: AuthValue = {
    user: session.user,
    signOut: async () => setSession(await signOut()),
  };
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
