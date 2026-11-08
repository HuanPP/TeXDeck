import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { ChevronDown, ExternalLink } from "lucide-react";
import {
  cancelClaudeLogin,
  finishChatGptLogin,
  getChatGptModels,
  getModelStatus,
  removeApiKey,
  saveApiKey,
  selectProvider,
  setProviderModel,
  signOutChatGpt,
  startChatGptLogin,
  startClaudeLogin,
  submitClaudeCode,
} from "../api";
import { modelReady, useModel } from "../model";
import type { Provider } from "../types";

const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));
const CLAUDE_CODE_SETUP = "https://code.claude.com/docs/en/setup";
const NAMES: Record<Provider, string> = { chatgpt: "ChatGPT", "claude-code": "Claude", api: "API key" };

type Login = { kind: "chatgpt" | "claude"; url: string };

function Row(props: {
  name: string;
  blurb: string;
  detail: ReactNode;
  model?: ReactNode;
  action: ReactNode;
  links?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <div className="border-t border-[var(--color-border)] py-3 first:border-t-0 first:pt-0">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-sm text-[var(--color-heading)]">{props.name}</div>
          <div className="text-xs text-slate-500">{props.blurb}</div>
          <div className="mt-1 text-xs text-slate-600">{props.detail}</div>
          {props.model && <div className="mt-2">{props.model}</div>}
          {props.links && <div className="mt-1 flex gap-3 text-xs">{props.links}</div>}
        </div>
        <div className="shrink-0">{props.action}</div>
      </div>
      {props.children && <div className="mt-3">{props.children}</div>}
    </div>
  );
}

const linkBtn = "text-[var(--color-primary)] hover:underline disabled:opacity-50";

function ModelSelect(props: {
  value: string;
  options: { value: string; label: string }[];
  disabled: boolean;
  onChange: (model: string) => void;
}) {
  const options = props.options.some((o) => o.value === props.value)
    ? props.options
    : [{ value: props.value, label: props.value }, ...props.options];
  return (
    <label className="flex items-center gap-2 text-xs text-slate-600">
      Model
      <select className="max-w-[16rem] rounded border border-[var(--color-border)] bg-white px-2 py-1 text-xs text-[var(--color-on-surface)]"
        value={props.value} disabled={props.disabled}
        onChange={(e) => props.onChange(e.target.value)}>
        {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
    </label>
  );
}

// Header control: one button showing the model in use; its panel connects
// ChatGPT, Claude (through Claude Code) or an API key and switches between them.
export default function ModelMenu() {
  const { status, loadError, setStatus, panelOpen, setPanelOpen } = useModel();
  const [login, setLogin] = useState<Login | null>(null);
  const [pasted, setPasted] = useState("");
  const [apiForm, setApiForm] = useState<{ api_key: string; base_url: string; model: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [chatgptModels, setChatgptModels] = useState<{ value: string; label: string }[]>([]);
  const rootRef = useRef<HTMLDivElement>(null);
  const pollRef = useRef(0);

  useEffect(() => () => window.clearInterval(pollRef.current), []);

  useEffect(() => {
    if (!panelOpen) return;
    const onDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setPanelOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setPanelOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [panelOpen, setPanelOpen]);

  // The ChatGPT list depends on the account, so fetch it whenever the panel
  // opens signed in; on failure the select still offers the current model.
  const chatgptSignedIn = status?.chatgpt.signed_in ?? false;
  useEffect(() => {
    if (!panelOpen || !chatgptSignedIn) return;
    getChatGptModels().then((r) => setChatgptModels(r.models)).catch(() => setChatgptModels([]));
  }, [panelOpen, chatgptSignedIn]);

  // While a sign-in is open, poll until it completes on its own (the ChatGPT
  // loopback listener, or Claude Code finishing in a browser tab it opened).
  useEffect(() => {
    if (!login) return;
    pollRef.current = window.setInterval(() => {
      getModelStatus()
        .then((s) => {
          setStatus(s);
          const side = login.kind === "chatgpt" ? s.chatgpt : s.claude;
          if (!side.pending) {
            setLogin(null);
            if (!side.signed_in) {
              setError((login.kind === "claude" && s.claude.login_error) || "Sign-in did not complete; try again.");
            }
          }
        })
        .catch(() => undefined);
    }, 2_000);
    return () => window.clearInterval(pollRef.current);
  }, [login, setStatus]);

  async function run(action: () => Promise<void>) {
    setError(null);
    setBusy(true);
    try {
      await action();
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  const closeForms = () => {
    if (login?.kind === "claude") cancelClaudeLogin().then(setStatus).catch(() => undefined);
    setLogin(null);
    setApiForm(null);
    setPasted("");
  };

  const connectChatGpt = () =>
    run(async () => {
      closeForms();
      const { url } = await startChatGptLogin();
      setLogin({ kind: "chatgpt", url });
      window.open(url, "_blank", "noopener");
    });

  // No window.open for Claude: Claude Code opens the browser itself when it
  // can, and the link shown is the fallback that ends on a page with a code.
  const connectClaude = () =>
    run(async () => {
      closeForms();
      const { url, ...next } = await startClaudeLogin();
      setStatus(next);
      if (url) setLogin({ kind: "claude", url });
    });

  const use = (provider: Provider) => run(async () => setStatus(await selectProvider(provider)));
  const pickModel = (provider: Provider, model: string) =>
    run(async () => setStatus(await setProviderModel(provider, model)));

  const submitPasted = (e: FormEvent) => {
    e.preventDefault();
    if (!login || !pasted.trim()) return;
    const current = login;
    return run(async () => {
      if (current.kind === "chatgpt") {
        setStatus(await finishChatGptLogin(pasted.trim()));
      } else {
        setLogin(null); // Claude Code exits after one code, right or wrong
        setStatus(await submitClaudeCode(pasted.trim()));
      }
      setLogin(null);
      setPasted("");
    });
  };

  const submitApi = (e: FormEvent) => {
    e.preventDefault();
    if (!apiForm) return;
    return run(async () => {
      setStatus(await saveApiKey(apiForm));
      setApiForm(null);
    });
  };

  if (!status) {
    const shown = loadError || error;
    return shown ? <span className="text-xs text-red-600">{shown}</span> : null;
  }

  const { chatgpt, claude, api } = status;
  const ready = modelReady(status);
  const label = ready ? `${NAMES[status.provider]} · ${status.model}` : "Connect a model";

  const inUse = <span className="text-xs text-[var(--color-primary)]">In use</span>;
  const useBtn = (provider: Provider) =>
    status.locked ? null : (
      <button type="button" className="btn-secondary px-2 py-1 text-xs" disabled={busy} onClick={() => use(provider)}>
        Use
      </button>
    );
  const connectBtn = (onClick: () => void, text = "Connect") => (
    <button type="button" className="btn-primary px-2 py-1 text-xs" disabled={busy} onClick={onClick}>
      {text}
    </button>
  );

  const pasteForm = (labelText: string, placeholder: string) => (
    <form onSubmit={submitPasted} className="space-y-2 text-xs">
      <a href={login!.url} target="_blank" rel="noopener noreferrer"
        className="inline-flex items-center gap-1 text-[var(--color-primary)]">
        Open the sign-in page <ExternalLink size={12} />
      </a>
      <label htmlFor="sign-in-paste" className="block text-slate-600">{labelText}</label>
      <input id="sign-in-paste" className="input-field text-xs" value={pasted}
        onChange={(e) => setPasted(e.target.value)} placeholder={placeholder} />
      <div className="flex justify-end gap-2">
        <button type="button" className="btn-secondary px-2 py-1 text-xs" onClick={closeForms}>Cancel</button>
        <button type="submit" className="btn-primary px-2 py-1 text-xs" disabled={busy || !pasted.trim()}>
          Finish sign-in
        </button>
      </div>
    </form>
  );

  return (
    <div ref={rootRef} className="relative text-sm">
      <button type="button" className="btn-secondary px-3 py-1 text-xs" onClick={() => setPanelOpen((o) => !o)}
        aria-expanded={panelOpen}>
        <span className={`inline-block h-2 w-2 rounded-full ${ready ? "bg-emerald-500" : "bg-red-500"}`} />
        <span className={ready ? "" : "text-red-600"}>{label}</span>
        <ChevronDown size={14} />
      </button>

      {panelOpen && (
        <div className="absolute right-0 top-full z-20 mt-2 w-[26rem] rounded-md border border-[var(--color-border)] bg-[var(--color-surface-container)] p-4 shadow-lg">
          <Row
            name="ChatGPT"
            blurb="Your ChatGPT plan"
            detail={chatgpt.signed_in ? `Signed in${chatgpt.email ? ` as ${chatgpt.email}` : ""}` : "Not connected"}
            model={chatgpt.signed_in && (
              <ModelSelect value={chatgpt.model} disabled={busy}
                options={chatgptModels}
                onChange={(m) => pickModel("chatgpt", m)} />
            )}
            action={status.provider === "chatgpt" && chatgpt.signed_in ? inUse
              : chatgpt.signed_in ? useBtn("chatgpt")
                : login?.kind === "chatgpt" ? null : connectBtn(connectChatGpt)}
            links={chatgpt.signed_in && (
              <button type="button" className={linkBtn} disabled={busy}
                onClick={() => run(async () => setStatus(await signOutChatGpt()))}>
                Sign out
              </button>
            )}
          >
            {login?.kind === "chatgpt" && (
              <>
                <p className="mb-2 text-xs text-slate-600">
                  Finish signing in on the ChatGPT page; this updates on its own. If the browser ends on a
                  page that will not load (TeXDeck on another machine or in Docker), paste that page's
                  address below.
                </p>
                {pasteForm("Address of the page the browser ended on:", "http://127.0.0.1:.../auth/callback?code=...")}
              </>
            )}
          </Row>

          <Row
            name="Claude"
            blurb="Your Claude plan, through Claude Code on this computer"
            detail={!claude.available ? "Claude Code is not installed here"
              : claude.signed_in
                ? [claude.email, claude.subscription].filter(Boolean).join(" · ") || "Signed in"
                : "Not connected"}
            model={claude.available && claude.signed_in && (
              <ModelSelect value={claude.model} options={claude.models} disabled={busy}
                onChange={(m) => pickModel("claude-code", m)} />
            )}
            action={!claude.available ? (
              <a href={CLAUDE_CODE_SETUP} target="_blank" rel="noopener noreferrer"
                className="btn-secondary px-2 py-1 text-xs">
                Install <ExternalLink size={12} />
              </a>
            ) : status.provider === "claude-code" ? inUse
              : claude.signed_in ? useBtn("claude-code")
                : login?.kind === "claude" ? null : connectBtn(connectClaude)}
          >
            {login?.kind === "claude" && (
              <>
                <p className="mb-2 text-xs text-slate-600">
                  If a Claude page opened by itself, finish there; this updates on its own. Otherwise open
                  the link, approve, and paste the code the page shows.
                </p>
                {pasteForm("Code from the Claude page:", "Paste the code")}
              </>
            )}
          </Row>

          <Row
            name="API key"
            blurb="Any OpenAI-compatible vision model"
            detail={api.configured ? `${api.model} · key ${api.key_hint}` : "Not set"}
            action={status.provider === "api" && api.configured ? inUse
              : api.configured ? useBtn("api")
                : apiForm ? null
                  : connectBtn(() => {
                    closeForms();
                    setApiForm({ api_key: "", base_url: api.base_url, model: api.model });
                  }, "Add key")}
            links={api.configured && !apiForm && (
              <>
                <button type="button" className={linkBtn} disabled={busy} onClick={() => {
                  closeForms();
                  setApiForm({ api_key: "", base_url: api.base_url, model: api.model });
                }}>
                  Edit
                </button>
                {api.saved && (
                  <button type="button" className={linkBtn} disabled={busy}
                    onClick={() => run(async () => setStatus(await removeApiKey()))}>
                    Remove
                  </button>
                )}
              </>
            )}
          >
            {apiForm && (
              <form onSubmit={submitApi} className="space-y-2 text-xs">
                <label className="block text-slate-600">
                  API key
                  <input type="password" className="input-field mt-1 text-xs" value={apiForm.api_key}
                    autoComplete="off"
                    placeholder={api.configured ? "Leave blank to keep the current key" : "sk-..."}
                    onChange={(e) => setApiForm({ ...apiForm, api_key: e.target.value })} />
                </label>
                <label className="block text-slate-600">
                  Base URL
                  <input className="input-field mt-1 text-xs" value={apiForm.base_url}
                    onChange={(e) => setApiForm({ ...apiForm, base_url: e.target.value })} />
                </label>
                <label className="block text-slate-600">
                  Model (must accept images)
                  <input className="input-field mt-1 text-xs" value={apiForm.model}
                    onChange={(e) => setApiForm({ ...apiForm, model: e.target.value })} />
                </label>
                <div className="flex justify-end gap-2">
                  <button type="button" className="btn-secondary px-2 py-1 text-xs" onClick={() => setApiForm(null)}>
                    Cancel
                  </button>
                  <button type="submit" className="btn-primary px-2 py-1 text-xs"
                    disabled={busy || !apiForm.base_url.trim() || !apiForm.model.trim()
                      || (!api.configured && !apiForm.api_key.trim())}>
                    Save and use
                  </button>
                </div>
              </form>
            )}
          </Row>

          {status.locked && (
            <p className="mt-2 text-xs text-slate-500">
              The server pins the model with TEXDECK_LLM_PROVIDER, so switching here is off.
            </p>
          )}
          {error && (
            <p className="mt-2 text-xs text-red-600">
              {error}{" "}
              <button type="button" className="underline" onClick={() => setError(null)}>Dismiss</button>
            </p>
          )}
        </div>
      )}
    </div>
  );
}
