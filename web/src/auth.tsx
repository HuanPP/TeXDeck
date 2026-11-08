import { createContext, useContext, useEffect, useState, type FormEvent, type ReactNode } from "react";
import { getSession, register, SIGNED_OUT_EVENT, signIn, signOut, type Session } from "./api";

// Sign-in gate. A TeXDeck without accounts (local, desktop) passes straight
// through; one with accounts shows only the sign-in form until it succeeds.
// With registration on, the form also signs up with an invite code.
type AuthValue = { user: string | null; admin: boolean; signOut: () => Promise<void> };
const AuthContext = createContext<AuthValue>({ user: null, admin: false, signOut: async () => undefined });

export const useAuth = () => useContext(AuthContext);

function SignInForm({ registration, onDone }: { registration: boolean; onDone: (session: Session) => void }) {
  const [signingUp, setSigningUp] = useState(false);
  const [invite, setInvite] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      onDone(signingUp ? await register(invite, username, password) : await signIn(username, password));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setPassword("");
    } finally {
      setBusy(false);
    }
  }

  function switchMode() {
    setSigningUp((v) => !v);
    setError(null);
  }

  const ready = username.trim() && password && (!signingUp || invite.trim());
  return (
    <div className="flex min-h-screen items-center justify-center px-6">
      <form onSubmit={submit} className="app-surface w-full max-w-sm space-y-4 p-6">
        <h1 className="text-2xl">TeXDeck</h1>
        {signingUp && (
          <div>
            <label className="mb-2 block text-sm text-slate-600" htmlFor="invite">Invite code</label>
            <input id="invite" className="input-field" autoComplete="off" autoFocus
              value={invite} onChange={(e) => setInvite(e.target.value)} />
          </div>
        )}
        <div>
          <label className="mb-2 block text-sm text-slate-600" htmlFor="username">Username</label>
          <input id="username" className="input-field" autoComplete="username" autoFocus={!signingUp}
            value={username} onChange={(e) => setUsername(e.target.value)} />
        </div>
        <div>
          <label className="mb-2 block text-sm text-slate-600" htmlFor="password">Password</label>
          <input id="password" type="password" className="input-field"
            autoComplete={signingUp ? "new-password" : "current-password"}
            value={password} onChange={(e) => setPassword(e.target.value)} />
          {signingUp && <p className="mt-1 text-xs text-slate-500">At least 8 characters.</p>}
        </div>
        {error && (
          <div role="alert" className="border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>
        )}
        <button type="submit" className="btn-primary w-full" disabled={busy || !ready}>
          {signingUp ? (busy ? "Creating account…" : "Create account") : busy ? "Signing in…" : "Sign in"}
        </button>
        {registration && (
          <p className="text-center text-sm text-slate-500">
            {signingUp ? "Already have an account? " : "Have an invite code? "}
            <button type="button" className="text-[var(--color-primary)] hover:underline" onClick={switchMode}>
              {signingUp ? "Sign in" : "Create an account"}
            </button>
          </p>
        )}
      </form>
    </div>
  );
}

export function AuthGate({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getSession().then(setSession).catch((e) => setError(e instanceof Error ? e.message : String(e)));
    const onSignedOut = () => setSession((s) => (s ? { ...s, user: null, admin: false } : s));
    window.addEventListener(SIGNED_OUT_EVENT, onSignedOut);
    return () => window.removeEventListener(SIGNED_OUT_EVENT, onSignedOut);
  }, []);

  if (error) return <p className="p-6 text-sm text-red-600">{error}</p>;
  if (!session) return null;
  if (session.enabled && !session.user) {
    return <SignInForm registration={session.registration} onDone={setSession} />;
  }

  const value: AuthValue = {
    user: session.user,
    admin: session.admin,
    signOut: async () => setSession(await signOut()),
  };
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
