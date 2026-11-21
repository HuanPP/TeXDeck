import { useCallback, useEffect, useRef, useState } from "react";
import { Check, ChevronDown, ChevronRight, History, Loader2, Plus, Send, Square, Trash2, Wrench, X } from "lucide-react";
import ChatMarkdown from "./ChatMarkdown";
import { useAsk } from "./AskDialog";
import { useModel } from "../model";
import {
  cancelCodexLogin, codexLoginState, deleteAgentSession, getAgent, getTranscript, startAgent, startCodexLogin, stopAgent,
  type AgentContext, type AgentItem, type AgentKind, type AgentSession, type AgentState, type CodexLogin,
} from "../paperApi";

const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));
const KIND_KEY = "texdeck.paper.agent";
const MODEL_KEY = "texdeck.paper.model.";
const tokens = (n: number) => (n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(n));

const when = (t: number) => {
  const d = new Date(t * 1000);
  const today = new Date().toDateString() === d.toDateString();
  return today ? d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" })
    : d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
};

// Past sessions, from a small icon at the panel's top right: whole titles, the
// agent and when each was last used; each can be opened or deleted.
function SessionMenu({ sessions, current, running, labels, onPick, onDelete }: {
  sessions: AgentSession[]; current: string; running: string; labels: Record<string, string>;
  onPick: (id: string) => void; onDelete: (session: AgentSession) => void;
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
  return (
    <div ref={box} className="relative">
      <button type="button" aria-haspopup="menu" aria-expanded={open} aria-label="Sessions" title="Sessions"
        disabled={!sessions.length}
        className={`flex h-7 items-center gap-1 bg-white/90 px-1.5 text-xs ${open ? "bg-slate-100 text-slate-900" : "text-slate-500 hover:bg-slate-100 hover:text-slate-900"} disabled:opacity-30`}
        onClick={() => setOpen((o) => !o)}>
        <History className="h-4 w-4" />
        {sessions.length > 0 && <span className="tabular-nums">{sessions.length}</span>}
      </button>
      {open && (
        <ul role="menu" className="absolute right-0 top-full z-30 mt-1 max-h-96 w-[min(22rem,80vw)] overflow-auto border border-[var(--color-border)] bg-white py-1 text-xs shadow-lg">
          {sessions.map((x) => (
            <li key={x.id} role="none" className={`group flex items-start ${x.id === current ? "bg-sky-50" : "hover:bg-slate-50"}`}>
              <button type="button" role="menuitem" className="flex min-w-0 flex-1 items-start gap-2 px-3 py-2 text-left"
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
              {x.id !== running && (
                <button type="button" title="Delete this session" aria-label={`Delete session ${x.title}`}
                  className="invisible mr-1 mt-1.5 p-1 text-slate-400 hover:text-red-600 group-hover:visible"
                  onClick={() => { setOpen(false); onDelete(x); }}>
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// A select only as wide as the chosen label: the label is drawn, and the native
// select lies invisibly on top of it to open the browser's own list.
function Pick({ label, value, options, onChange, disabled, title }: {
  label: string; value: string; options: { value: string; label: string }[];
  onChange: (value: string) => void; disabled?: boolean; title?: string;
}) {
  const shown = options.find((o) => o.value === value)?.label ?? value;
  return (
    <span title={title}
      className={`relative inline-flex min-w-0 items-center gap-0.5 px-1.5 py-1 text-xs ${
        disabled ? "text-slate-400" : "text-slate-600 hover:bg-slate-100 hover:text-slate-900"}`}>
      <span className="truncate">{shown}</span>
      {!disabled && <ChevronDown className="h-3 w-3 shrink-0 text-slate-400" />}
      <select aria-label={label} value={value} disabled={disabled} onChange={(e) => onChange(e.target.value)}
        className="absolute inset-0 cursor-pointer opacity-0 disabled:cursor-default">
        {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
    </span>
  );
}

// Notices from the CLI (warnings, not failures): a card over the transcript
// until dismissed, never part of it.
const DISMISSED_KEY = "texdeck.paper.dismissed";
function useDismissed(): [Set<string>, (key: string) => void] {
  const [set, setSet] = useState(() => {
    try { return new Set<string>(JSON.parse(localStorage.getItem(DISMISSED_KEY) ?? "[]")); } catch { return new Set<string>(); }
  });
  const dismiss = (key: string) => setSet((prev) => {
    const next = new Set(prev).add(key);
    localStorage.setItem(DISMISSED_KEY, JSON.stringify([...next].slice(-300)));
    return next;
  });
  return [set, dismiss];
}

function ToolRow({ item }: { item: ToolItem }) {
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

type ToolItem = Extract<AgentItem, { kind: "tool" }>;

// Two or more tool calls in a row fold into one line ("5 steps · shell 3, edit 1").
// While the agent is still in that run of calls it stays open; afterwards it folds,
// unless the user opened or closed it themselves.
function ToolGroup({ tools, live }: { tools: ToolItem[]; live: boolean }) {
  const [choice, setChoice] = useState<boolean | null>(null);
  const open = choice ?? live;
  const counts = new Map<string, number>();
  for (const t of tools) counts.set(t.name, (counts.get(t.name) ?? 0) + 1);
  const summary = [...counts].map(([name, n]) => (n > 1 ? `${name} ${n}` : name)).join(", ");
  const failed = tools.filter((t) => t.status === "error").length;
  return (
    <div className="text-[11.5px]">
      <button type="button" aria-expanded={open} onClick={() => setChoice(!open)}
        className="flex w-full min-w-0 items-center gap-1.5 text-left text-slate-500 hover:text-slate-800">
        {live ? <Loader2 className="h-3 w-3 shrink-0 animate-spin text-sky-600" /> : <Wrench className="h-3 w-3 shrink-0" />}
        <span className="shrink-0 font-medium">{tools.length} steps</span>
        <span className="min-w-0 truncate text-slate-400">{summary}</span>
        {failed > 0 && <span className="shrink-0 text-red-600">{failed} failed</span>}
        {open ? <ChevronDown className="ml-auto h-3 w-3 shrink-0" /> : <ChevronRight className="ml-auto h-3 w-3 shrink-0" />}
      </button>
      {open && (
        <div className="mt-1.5 space-y-1.5 border-l border-[var(--color-border)] pl-2.5">
          {tools.map((t, i) => <ToolRow key={t.key + i} item={t} />)}
        </div>
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
          case "tool": {
            // Only the first call of a run draws anything; it draws the whole run.
            // Notices are drawn elsewhere, so they don't break a run.
            let start = i;
            while (start > 0 && items[start - 1].kind === "notice") start--;
            if (start > 0 && items[start - 1].kind === "tool") return null;
            let end = i;
            while (end < items.length && (items[end].kind === "tool" || items[end].kind === "notice")) end++;
            const tools = items.slice(i, end).filter((x): x is ToolItem => x.kind === "tool");
            if (tools.length === 1) return <ToolRow key={item.key + i} item={item} />;
            const live = running && (end === items.length || tools.some((t) => t.status === "running"));
            return <ToolGroup key={`g${i}`} tools={tools} live={live} />;
          }
          case "notice":
            return null; // shown as a dismissible card instead
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
  const [picked, setModels] = useState<Partial<Record<AgentKind, string>>>({});
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
  const models = option?.models ?? [];
  const stored = picked[chosen] ?? localStorage.getItem(MODEL_KEY + chosen) ?? "";
  const model = models.some((m) => m.value === stored) ? stored
    : models.some((m) => m.value === option?.model) ? option!.model : models[0]?.value ?? "";
  const busy = !!running;
  const here = busy && running === session;

  const send = async () => {
    const text = prompt.trim();
    if (!text || busy || sending) return;
    setSending(true);
    setError("");
    try {
      const r = await startAgent(id, chosen, text, session === "new" ? "" : session, context(),
        chosen === "api" || model === "default" ? "" : model);
      setPrompt("");
      stick.current = true;
      setSession(r.session);
      await load();
    } catch (e) { setError(errMsg(e)); }
    finally { setSending(false); }
  };

  const removeSession = async (target: AgentSession) => {
    const ok = await ask({ title: "Delete this session?", message: `"${target.title}" and its history will be deleted. The project's files stay as they are.`,
      confirm: "Delete", danger: true });
    if (ok === null) return;
    try {
      await deleteAgentSession(id, target.id);
      if (target.id === session) setSession("new");
      await load();
    } catch (e) { setError(errMsg(e)); }
  };
  const [dismissed, dismiss] = useDismissed();
  const notices = items.flatMap((item, i) =>
    item.kind === "notice" && !dismissed.has(`${session}:${i}`) ? [{ key: `${session}:${i}`, text: item.text }] : []);

  if (!state) return <p className="p-4 text-sm text-slate-400">{error || "Loading…"}</p>;
  const kinds = Object.keys(state.agents) as AgentKind[];

  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      {askDialog}
      <div className="absolute right-2 top-1.5 z-20 flex items-center gap-0.5">
        <SessionMenu sessions={state.sessions} current={session} running={running}
          labels={Object.fromEntries(kinds.map((k) => [k, state.agents[k].label]))}
          onPick={(sid) => { stick.current = true; setSession(sid); }} onDelete={(x) => void removeSession(x)} />
        <button type="button" title="New session" aria-label="New session" disabled={session === "new"}
          className="flex h-7 w-7 items-center justify-center bg-white/90 text-slate-500 hover:bg-slate-100 hover:text-slate-900 disabled:opacity-30"
          onClick={() => setSession("new")}><Plus className="h-4 w-4" /></button>
      </div>
      {notices.length > 0 && (
        <div className="absolute inset-x-2 top-10 z-20 space-y-1.5">
          {notices.map((n) => (
            <div key={n.key} role="status" className="flex items-start gap-2 border border-amber-200 bg-amber-50/95 px-3 py-2 text-[11.5px] text-amber-800 shadow-md">
              <span className="min-w-0 flex-1 break-words">{n.text}</span>
              <button type="button" aria-label="Dismiss" title="Dismiss" className="shrink-0 p-0.5 text-amber-600 hover:text-amber-900"
                onClick={() => dismiss(n.key)}><X className="h-3.5 w-3.5" /></button>
            </div>
          ))}
        </div>
      )}

      <div ref={scroller} className="min-h-0 flex-1 overflow-y-auto px-3 pb-3 pt-10"
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
      <form className="shrink-0 p-2" onSubmit={(e) => { e.preventDefault(); void send(); }}>
        <div className="border border-[var(--color-border)] bg-white focus-within:border-[var(--color-primary)]">
          <textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} rows={3}
            placeholder={busy && !here ? "The agent is working in another session…" : "Ask the agent… (Enter to send, Shift+Enter for a new line)"}
            aria-label="Message to the agent"
            className="block w-full resize-none border-0 bg-transparent px-2.5 pt-2 text-[13px] outline-none"
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void send(); }
            }} />
          <div className="flex items-center gap-1 px-1.5 pb-1.5">
            <Pick label="Agent" value={chosen} disabled={!!current}
              title={current ? "A session keeps the agent it started with; start a new session to switch" : "Agent"}
              options={kinds.map((k) => ({ value: k, label: state.agents[k].label + (state.agents[k].available ? "" : " (not connected)") }))}
              onChange={(v) => { setKind(v as AgentKind); localStorage.setItem(KIND_KEY, v); }} />
            <span className="flex-1" />
            {models.length > 0 && (
              <Pick label="Model" value={model} options={models} disabled={chosen === "api"}
                title={chosen === "api" ? "The API key's model (change it in the model menu)" : "Model"}
                onChange={(v) => { setModels((m) => ({ ...m, [chosen]: v })); localStorage.setItem(MODEL_KEY + chosen, v); }} />
            )}
            {busy ? (
              <button type="button" title="Stop" aria-label="Stop"
                className="flex h-7 items-center gap-1 bg-slate-700 px-2.5 text-xs text-white hover:bg-slate-900"
                onClick={() => void stopAgent(id)}>
                <Square className="h-3 w-3 fill-current" /> Stop
              </button>
            ) : (
              <button type="submit" title="Send" aria-label="Send"
                className="flex h-7 w-7 items-center justify-center bg-[var(--color-primary)] text-white hover:bg-[var(--color-primary-hover)] disabled:opacity-40"
                disabled={!prompt.trim() || sending || (!option?.available && !current)}>
                <Send className="h-3.5 w-3.5" />
              </button>
            )}
          </div>
        </div>
      </form>
    </div>
  );
}
