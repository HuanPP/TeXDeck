import { useEffect, useRef, useState, type FormEvent } from "react";
import { ExternalLink, LogOut } from "lucide-react";
import { finishChatGptLogin, getModelStatus, signOutChatGpt, startChatGptLogin } from "../api";
import type { ModelStatus } from "../types";

const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));

// Header control: which model generation will use, and ChatGPT sign-in/out.
export default function ModelMenu() {
  const [status, setStatus] = useState<ModelStatus | null>(null);
  const [loginUrl, setLoginUrl] = useState<string | null>(null);
  const [pasted, setPasted] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const pollRef = useRef(0);

  useEffect(() => {
    getModelStatus().then(setStatus).catch((e) => setError(errMsg(e)));
    return () => window.clearInterval(pollRef.current);
  }, []);

  // While a sign-in is open, poll until the loopback listener completes it.
  useEffect(() => {
    if (!loginUrl) return;
    pollRef.current = window.setInterval(() => {
      getModelStatus()
        .then((s) => {
          setStatus(s);
          if (s.signed_in || !s.pending) {
            setLoginUrl(null);
            if (!s.signed_in) setError("Sign-in did not complete; try again.");
          }
        })
        .catch(() => undefined);
    }, 2_000);
    return () => window.clearInterval(pollRef.current);
  }, [loginUrl]);

  async function signIn() {
    setError(null);
    setBusy(true);
    try {
      const { url } = await startChatGptLogin();
      setLoginUrl(url);
      setPasted("");
      window.open(url, "_blank", "noopener");
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  async function submitPasted(e: FormEvent) {
    e.preventDefault();
    if (!pasted.trim()) return;
    setError(null);
    setBusy(true);
    try {
      setStatus(await finishChatGptLogin(pasted.trim()));
      setLoginUrl(null);
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  async function signOut() {
    setError(null);
    try {
      setStatus(await signOutChatGpt());
    } catch (e) {
      setError(errMsg(e));
    }
  }

  if (!status) {
    return error ? <span className="text-xs text-red-600">{error}</span> : null;
  }

  const usingChatGpt = status.provider === "chatgpt";
  const label = usingChatGpt
    ? `ChatGPT · ${status.model}`
    : status.api_key_configured
      ? `API · ${status.model}`
      : "No model configured";

  return (
    <div className="relative flex items-center gap-3 text-sm">
      <span
        className={status.api_key_configured || usingChatGpt ? "text-slate-600" : "text-red-600"}
        title={status.email || undefined}
      >
        {label}
      </span>
      {status.signed_in ? (
        <button type="button" className="btn-secondary px-2 py-1 text-xs" onClick={signOut}
          title={status.email ? `Signed in as ${status.email}` : undefined}>
          <LogOut size={14} /> Sign out
        </button>
      ) : (
        <button type="button" className="btn-secondary px-2 py-1 text-xs" onClick={signIn} disabled={busy}>
          Sign in with ChatGPT
        </button>
      )}

      {(loginUrl || error) && (
        <div className="absolute right-0 top-full z-20 mt-2 w-96 rounded-md border border-[var(--color-border)] bg-[var(--color-surface-container)] p-4 text-xs text-slate-600 shadow-lg">
          {loginUrl && (
            <>
              <p className="mb-2">
                Finish signing in on the ChatGPT page. This updates on its own when you are done.
              </p>
              <a href={loginUrl} target="_blank" rel="noopener noreferrer"
                className="mb-3 inline-flex items-center gap-1 text-[var(--color-primary)]">
                Open the sign-in page <ExternalLink size={12} />
              </a>
              <form onSubmit={submitPasted} className="space-y-2">
                <label htmlFor="callback-url" className="block">
                  If the browser ends on a page that will not load (TeXDeck on another machine or in
                  Docker), paste that page's full address here:
                </label>
                <input id="callback-url" className="input-field text-xs" value={pasted}
                  onChange={(e) => setPasted(e.target.value)}
                  placeholder="http://127.0.0.1:.../auth/callback?code=..." />
                <div className="flex justify-end gap-2">
                  <button type="button" className="btn-secondary px-2 py-1 text-xs"
                    onClick={() => setLoginUrl(null)}>
                    Cancel
                  </button>
                  <button type="submit" className="btn-primary px-2 py-1 text-xs" disabled={busy || !pasted.trim()}>
                    Finish sign-in
                  </button>
                </div>
              </form>
            </>
          )}
          {error && (
            <p className={loginUrl ? "mt-2 text-red-600" : "text-red-600"}>
              {error}{" "}
              {!loginUrl && (
                <button type="button" className="underline" onClick={() => setError(null)}>Dismiss</button>
              )}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
