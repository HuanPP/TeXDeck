import { useCallback, useEffect, useRef, useState } from "react";
import { Check, ChevronDown, ChevronRight, Loader2, Plus, Send, Square, Trash2, Wrench } from "lucide-react";
import ChatMarkdown from "./ChatMarkdown";
import { useAsk } from "./AskDialog";
import { useModel } from "../model";
import {
  cancelCodexLogin, codexLoginState, deleteAgentSession, getAgent, getTranscript, startAgent, startCodexLogin, stopAgent,
  type AgentContext, type AgentItem, type AgentKind, type AgentSession, type AgentState, type CodexLogin,
} from "../paperApi";

const when = (t: number) => {
  const d = new Date(t * 1000);
  const today = new Date().toDateString() === d.toDateString();
  return today ? d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" })
    : d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
};

// The session picker: full titles (two lines), the agent and when it was last used.
function SessionMenu({ sessions, current, running, labels, onPick }: {
  sessions: AgentSession[]; current: string; running: string; labels: Record<string, string>; onPick: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => { if (!box.current?.contains(e.target as Node)) setOpen(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    window.addEventListener("mousedown", close);
    window.addEventListener("keydown", esc);
    return () => { window.removeEventListener("mousedown", close); window.removeEventListener("keydown", esc); };
  }, [open]);
  const shown = sessions.find((x) => x.id === current);
  return (
    <div ref={box} className="relative min-w-0 flex-1">
      <button type="button" aria-haspopup="listbox" aria-expanded={open} aria-label="Agent session" title={shown?.title}
        className="flex w-full min-w-0 items-center gap-1 border border-[var(--color-border)] bg-white px-2 py-1 text-left hover:border-[var(--color-primary)]"
        onClick={() => setOpen((o) => !o)}>
        {shown?.id === running && <span className="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-sky-500" />}
        <span className="min-w-0 flex-1 truncate">{shown ? shown.title : "New session"}</span>
        <ChevronDown className="h-3.5 w-3.5 shrink-0 text-slate-400" />
      </button>
      {open && (
        <ul role="listbox" className="absolute left-0 top-full z-30 mt-1 max-h-80 w-[min(24rem,80vw)] overflow-auto border border-[var(--color-border)] bg-white py-1 shadow-lg">
          <li role="option" aria-selected={current === "new"}>
            <button type="button" className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-slate-50"
              onClick={() => { onPick("new"); setOpen(false); }}>
              <Plus className="h-3.5 w-3.5 text-slate-400" /> New session
            </button>
          </li>
          {sessions.map((x) => (
            <li key={x.id} role="option" aria-selected={x.id === current}>
              <button type="button" className={`flex w-full items-start gap-2 px-3 py-2 text-left hover:bg-slate-50 ${x.id === current ? "bg-sky-50" : ""}`}
                onClick={() => { onPick(x.id); setOpen(false); }}>
                <span className="mt-0.5 w-3.5 shrink-0">
                  {x.id === running ? <span className="mt-1 block h-1.5 w-1.5 animate-pulse rounded-full bg-sky-500" />
                    : x.id === current ? <Check className="h-3.5 w-3.5 text-[var(--color-primary)]" /> : null}
                </span>
                <span className="min-w-0 flex-1">
                  <span className="line-clamp-2 text-slate-800">{x.title}</span>
                  <span className="mt-0.5 block text-[10.5px] text-slate-400">{labels[x.agent] ?? x.agent} · {when(x.updated_at)}</span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));
const KIND_KEY = "texdeck.paper.agent";
const tokens = (n: number) => (n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(n));

function ToolRow({ item }: { item: Extract<AgentItem, { kind: "tool" }> }) {
  const [open, setOpen] = useState(false);
  const color = item.status === "error" ? "text-red-600" : item.status === "running" ? "text-sky-600" : "text-slate-500";
  return (
    <div className="text-[11.5px]">
      <button type="button" className={`flex w-full min-w-0 items-center gap-1.5 text-left ${color}`} disabled={!item.output}
        onClick={() => setOpen((o) => !o)}>
        {item.status === "running" ? <Loader2 className="h-3 w-3 shrink-0 animate-spin" /> : <Wrench className="h-3 w-3 shrink-0" />}
        <span className="shrink-0 font-medium">{item.name}</span>
        <span className="min-w-0 truncate font-mono text-slate-400" title={item.detail}>{item.detail}</span>
        {item.output && (open ? <ChevronDown className="ml-auto h-3 w-3 shrink-0" /> : <ChevronRight className="ml-auto h-3 w-3 shrink-0" />)}
      </button>
      {open && item.output && (
        <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap bg-slate-50 p-2 font-mono text-[10.5px] text-slate-600">{item.output}</pre>
      )}
    </div>
  );
}

// Agents link files as file:///work/<path>#L12(-L20) or /work/<path>; those open
// in the editor instead of a dead browser tab.
const FILE_LINK = /\]\((?:file:\/\/)?\/work\/([^)#\s]+)(?:#L(\d+)(?:-L?\d+)?)?\)/g;
const linkFiles = (text: string) => text.replace(FILE_LINK, (_, path: string, line?: string) =>
  `](#open:${path}${line ? `:${line}` : ""})`);

function Transcript({ items, running, onOpenFile }: {
  items: AgentItem[]; running: boolean; onOpenFile: (path: string, line?: number) => void;
}) {
  return (
    <div className="space-y-2.5" onClick={(e) => {
      const a = (e.target as HTMLElement).closest("a");
      const href = a?.getAttribute("href") ?? "";
      if (!href.startsWith("#open:")) return;
      e.preventDefault();
      const [path, line] = decodeURIComponent(href.slice(6)).split(":");
      onOpenFile(path, line ? Number(line) : undefined);
    }}>
      {items.map((item, i) => {
        switch (item.kind) {
          case "user":
            return <div key={i} className="ml-6 whitespace-pre-wrap bg-sky-50 px-3 py-2 text-[13px] text-slate-800">{item.text}</div>;
          case "text":
            return <div key={i} className="notebook-chat text-[13px]"><ChatMarkdown text={linkFiles(item.text)} /></div>;
          case "tool":
            return <ToolRow key={item.key + i} item={item} />;
          case "notice":
            return <p key={i} className="text-[11px] text-slate-400">{item.text}</p>;
          case "error":
            return <p key={i} role="alert" className="bg-red-50 px-2 py-1 text-xs text-red-700">{item.text}</p>;
          case "done":
            return (
              <p key={i} className="border-b border-dashed border-[var(--color-border)] pb-2 text-[10.5px] text-slate-400">
                {item.seconds}s{item.usage.input + item.usage.output > 0 && ` · ${tokens(item.usage.input)} in · ${tokens(item.usage.output)} out`}
              </p>
            );
        }
      })}
      {running && <p className="flex items-center gap-1.5 text-xs text-sky-600"><Loader2 className="h-3 w-3 animate-spin" /> Working…</p>}
    </div>
  );
}

// Codex has its own ChatGPT sign-in: a device code the user enters on OpenAI's site.
function CodexSignIn({ onDone }: { onDone: () => void }) {
  const [login, setLogin] = useState<CodexLogin | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!login?.active) return;
    const t = window.setInterval(async () => {
      try {
        const s = await codexLoginState();
        setLogin(s);
        if (s.signed_in) onDone();
      } catch (e) { setError(errMsg(e)); }
    }, 2000);
    return () => window.clearInterval(t);
  }, [login?.active, onDone]);
  if (!login) {
    return (
      <div className="space-y-2 text-xs text-slate-600">
        <p>Codex signs in to your ChatGPT plan with a one-time code.</p>
        <button type="button" className="btn-primary px-3 py-1 text-xs"
          onClick={() => startCodexLogin().then(setLogin, (e) => setError(errMsg(e)))}>Sign in to Codex</button>
        {error && <p className="text-red-600">{error}</p>}
      </div>
    );
  }
  return (
    <div className="space-y-2 text-xs text-slate-600">
      {login.url && login.code ? (
        <>
          <p>Open <a href={login.url} target="_blank" rel="noreferrer">{login.url}</a>, sign in, and enter this code:</p>
          <p className="select-all font-mono text-lg tracking-widest text-[var(--color-heading)]">{login.code}</p>
          <p className="text-slate-400">Waiting for you to approve it… (the code expires in 15 minutes)</p>
        </>
      ) : null}
      {(login.error || error) && <p className="text-red-600">{login.error || error}</p>}
      <button type="button" className="btn-secondary px-3 py-1 text-xs"
        onClick={() => { void cancelCodexLogin(); setLogin(null); }}>{login.active ? "Cancel" : "Start again"}</button>
    </div>
  );
}

export default function PaperAgent({ id, version, running, context, onOpenFile }: {
  id: string; version: number; running: string; context: () => AgentContext;
  onOpenFile: (path: string, line?: number) => void;
}) {
  const { setPanelOpen } = useModel();
  const [state, setState] = useState<AgentState | null>(null);
  const [kind, setKind] = useState<AgentKind | null>(() => localStorage.getItem(KIND_KEY) as AgentKind | null);
  const [session, setSession] = useState("");
  const [items, setItems] = useState<AgentItem[]>([]);
  const [prompt, setPrompt] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const scroller = useRef<HTMLDivElement>(null);
  const [ask, askDialog] = useAsk();
  const stick = useRef(true);

  const load = useCallback(async () => {
    try {
      const s = await getAgent(id);
      setState(s);
      setSession((cur) => (cur && (cur === "new" || s.sessions.some((x) => x.id === cur)) ? cur : s.running || s.sessions[0]?.id || "new"));
    } catch (e) { setError(errMsg(e)); }
  }, [id]);
  useEffect(() => { void load(); }, [load, version]);

  useEffect(() => {
    if (!session || session === "new") { setItems([]); return; }
    let live = true;
    getTranscript(id, session).then((t) => { if (live) setItems(t.items); }, (e) => live && setError(errMsg(e)));
    return () => { live = false; };
  }, [id, session, version]);

  useEffect(() => {
    const el = scroller.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [items]);

  const current = state?.sessions.find((s) => s.id === session);
  const chosen: AgentKind = current?.agent ?? kind ?? state?.default ?? "claude";
  const option = state?.agents[chosen];
  const busy = !!running;
  const here = busy && running === session;

  const send = async () => {
    const text = prompt.trim();
    if (!text || busy || sending) return;
    setSending(true);
    setError("");
    try {
      const r = await startAgent(id, chosen, text, session === "new" ? "" : session, context());
      setPrompt("");
      stick.current = true;
      setSession(r.session);
      await load();
    } catch (e) { setError(errMsg(e)); }
    finally { setSending(false); }
  };

  const removeSession = async () => {
    if (!current) return;
    const ok = await ask({ title: "Delete this session?", message: `"${current.title}" and its history will be deleted. The project's files stay as they are.`,
      confirm: "Delete", danger: true });
    if (ok === null) return;
    try {
      await deleteAgentSession(id, current.id);
      setSession("new");
      await load();
    } catch (e) { setError(errMsg(e)); }
  };

  if (!state) return <p className="p-4 text-sm text-slate-400">{error || "Loading…"}</p>;
  const kinds = Object.keys(state.agents) as AgentKind[];

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex shrink-0 flex-wrap items-center gap-1.5 border-b border-[var(--color-border)] px-2 py-1.5 text-xs">
        {askDialog}
        <SessionMenu sessions={state.sessions} current={session} running={running}
          labels={Object.fromEntries(kinds.map((k) => [k, state.agents[k].label]))}
          onPick={(sid) => { stick.current = true; setSession(sid); }} />
        <button type="button" title="New session" aria-label="New session" className="p-1 text-slate-500 hover:text-slate-900"
          onClick={() => setSession("new")}><Plus className="h-4 w-4" /></button>
        {current && !here && (
          <button type="button" title="Delete this session" aria-label="Delete this session" className="p-1 text-slate-400 hover:text-red-600"
            onClick={() => void removeSession()}><Trash2 className="h-4 w-4" /></button>
        )}
      </div>
      <div className="flex shrink-0 items-center gap-2 border-b border-[var(--color-border)] px-2 py-1 text-xs text-slate-500">
        Agent
        <select aria-label="Agent" value={chosen} disabled={!!current}
          title={current ? "A session keeps the agent it started with" : undefined}
          onChange={(e) => { setKind(e.target.value as AgentKind); localStorage.setItem(KIND_KEY, e.target.value); }}
          className="min-w-0 flex-1 border border-[var(--color-border)] bg-white px-1 py-0.5 disabled:bg-slate-50">
          {kinds.map((k) => (
            <option key={k} value={k}>
              {state.agents[k].label}{state.agents[k].model && state.agents[k].model !== "default" ? ` · ${state.agents[k].model}` : ""}
              {state.agents[k].available ? "" : " (not connected)"}
            </option>
          ))}
        </select>
        {current && <span className="shrink-0 text-[10.5px] text-slate-400">fixed for this session</span>}
      </div>

      <div ref={scroller} className="min-h-0 flex-1 overflow-y-auto px-3 py-3"
        onScroll={(e) => { const el = e.currentTarget; stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40; }}>
        {option && !option.available && !current ? (
          option.reason ? <p className="text-xs text-slate-500">{option.reason}</p>
            : chosen === "codex" ? <CodexSignIn onDone={() => void load()} />
            : (
              <div className="space-y-2 text-xs text-slate-600">
                <p>{option.label} is not connected for your account.</p>
                <button type="button" className="btn-secondary px-3 py-1 text-xs" onClick={() => setPanelOpen(true)}>
                  Connect it from the model menu
                </button>
              </div>
            )
        ) : items.length ? (
          <Transcript items={items} running={here} onOpenFile={onOpenFile} />
        ) : (
          <div className="space-y-2 text-xs leading-relaxed text-slate-500">
            <p>Ask the agent to write or change the project: it edits the files directly, and the PDF updates when it is done.</p>
            <p className="text-slate-400">It sees which file you have open, your cursor line and any text you selected, and the last compile error.</p>
          </div>
        )}
      </div>

      {error && <p role="alert" className="shrink-0 bg-red-50 px-3 py-1 text-xs text-red-700">{error}</p>}
      <form className="shrink-0 border-t border-[var(--color-border)] p-2" onSubmit={(e) => { e.preventDefault(); void send(); }}>
        <textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} rows={3}
          placeholder={busy && !here ? "The agent is working in another session…" : "Ask the agent… (Enter to send, Shift+Enter for a new line)"}
          aria-label="Message to the agent"
          className="block w-full resize-none border border-[var(--color-border)] px-2 py-1.5 text-[13px] outline-none focus:border-[var(--color-primary)]"
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void send(); }
          }} />
        <div className="mt-1.5 flex items-center justify-end gap-2">
          {busy ? (
            <button type="button" className="btn-secondary flex items-center gap-1 px-3 py-1 text-xs" onClick={() => void stopAgent(id)}>
              <Square className="h-3 w-3" /> Stop
            </button>
          ) : (
            <button type="submit" className="btn-primary flex items-center gap-1 px-3 py-1 text-xs"
              disabled={!prompt.trim() || sending || (!option?.available && !current)}>
              <Send className="h-3 w-3" /> Send
            </button>
          )}
        </div>
      </form>
    </div>
  );
}
