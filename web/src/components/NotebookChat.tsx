import { useEffect, useRef, useState } from "react";
import { ArrowUp, Eraser, GripVertical, MessageSquarePlus, NotebookPen, PanelRightClose, Trash2, X } from "lucide-react";
import type { ChatAnchor, ChatThread } from "../api";
import ChatMarkdown from "./ChatMarkdown";

// The notebook's AI column. Every question belongs to a thread: one started by
// quoting text gets a number, drawn by that text. Opening a number shows that
// thread on its own: on top of this column, or (dragged by its grip) over any
// half-column of the notebook; see NotebookWorkspace.

type Props = {
  threads: ChatThread[];
  quotes: ChatAnchor[]; // waiting in the box for the next question
  onRemoveQuote: (index: number) => void;
  onAsk: (question: string, thread: string | null) => Promise<void>;
  openThread: string | null;
  onOpenThread: (id: string | null) => void;
  onGoTo: (anchor: ChatAnchor) => void;
  onDelete: (id: string) => void;
  error: string | null;
  focusKey: number; // bumps when a quote is added, to focus the box
  imageUrl: (name: string) => string; // an area quote's crop
  onAddToNotes?: (thread: ChatThread, answerId: string) => void;
  onClear: () => Promise<void>; // New session
  docked: "top" | "bottom" | null; // where the open thread splits this column (null: it is elsewhere)
  onGripDown: (e: React.PointerEvent) => void; // start dragging the open thread away
};

export type View = { multiDoc: boolean; onGoTo: (a: ChatAnchor) => void; imageUrl: (name: string) => string;
  onAddToNotes?: (thread: ChatThread, answerId: string) => void };

const where = (a: ChatAnchor, multiDoc: boolean) =>
  (a.area ? "Area, " : "")
  + (a.where === "notes" ? `Notes p. ${a.page}` : `Source${multiDoc ? ` ${a.doc}` : ""} p. ${a.page}`);

function Badge({ n, onClick }: { n: number; onClick?: () => void }) {
  const cls = "inline-flex h-5 min-w-5 shrink-0 items-center justify-center rounded-full bg-indigo-500 px-1 font-sans text-[11px] leading-none text-white";
  return onClick
    ? <button type="button" className={`${cls} hover:bg-indigo-700`} title="Open beside the conversation" onClick={onClick}>{n}</button>
    : <span className={cls}>{n}</span>;
}

function Quotes({ anchors, multiDoc, onGoTo, imageUrl }: { anchors: ChatAnchor[] } & View) {
  return (
    <>
      {anchors.map((a, i) => (
        <blockquote key={i} className="border-l-2 border-indigo-300 pl-2 text-slate-500">
          <button type="button" className="mr-1 font-sans text-[11px] text-indigo-600 hover:underline disabled:text-slate-400 disabled:no-underline"
            disabled={a.gone} onClick={() => onGoTo(a)}>
            {where(a, multiDoc)}{a.gone ? " (no longer in the notes)" : ""}
          </button>
          {a.image
            ? <img src={imageUrl(a.image)} alt="Quoted area" className="mt-1 max-h-40 max-w-full border border-[var(--color-border)] bg-white" />
            : <span className="line-clamp-3">{a.text}</span>}
        </blockquote>
      ))}
    </>
  );
}

export const TEXT_SIZE = "text-[13px] leading-relaxed";
const OPEN_SIZE = "text-[12.5px] leading-relaxed";

// The opened thread: its header (grip to move it, close) and its messages.
export function OpenThread({ thread, view, onClose, onGripDown, onDockBack, fill, bottom }: {
  thread: ChatThread; view: View; onClose: () => void; onGripDown: (e: React.PointerEvent) => void;
  onDockBack?: () => void; // shown when it floats over another column
  fill?: boolean; // floating: takes the whole box
  bottom?: boolean; // in the lower half of the side panel
}) {
  const bodyRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => { bodyRef.current?.scrollTo({ top: 0 }); }, [thread.id]);
  const n = thread.messages.filter((m) => m.role === "user").length;
  return (
    <section className={`notebook-chat flex min-h-0 flex-col bg-indigo-50/60 ${fill ? "h-full" : bottom ? "basis-1/2 border-t-2 border-indigo-200" : "basis-1/2 border-b-2 border-indigo-200"}`}
      aria-label="Opened question">
      <div className="flex shrink-0 items-center gap-1.5 px-1.5 py-1.5">
        <button type="button" className="cursor-grab touch-none p-1 text-slate-400 hover:text-indigo-600 active:cursor-grabbing"
          aria-label="Drag to place this question over another part of the notebook"
          title="Drag over any half of a column to read it there" onPointerDown={onGripDown}>
          <GripVertical className="h-4 w-4" />
        </button>
        {thread.number > 0 && <Badge n={thread.number} />}
        <span className="min-w-0 flex-1 truncate text-xs text-slate-500">
          {n} {n === 1 ? "question" : "questions"} on this passage
        </span>
        {onDockBack && (
          <button type="button" className="p-1 text-slate-400 hover:text-slate-700" aria-label="Back to the side panel"
            title="Back to the side panel" onClick={onDockBack}>
            <PanelRightClose className="h-4 w-4" />
          </button>
        )}
        <button type="button" className="p-1 text-slate-400 hover:text-slate-700" aria-label="Close" onClick={onClose}>
          <X className="h-4 w-4" />
        </button>
      </div>
      <div ref={bodyRef} className={`min-h-0 flex-1 space-y-3 overflow-auto px-3 pb-3 ${OPEN_SIZE}`}>
        <Messages thread={thread} {...view} />
      </div>
    </section>
  );
}

function Messages({ thread, ...view }: { thread: ChatThread } & View) {
  return (
    <>
      {thread.messages.map((m) => m.role === "user" ? (
        <div key={m.id} className="space-y-1.5">
          <Quotes anchors={thread.anchors.filter((a) => a.msg === m.id)} {...view} />
          <div className="rounded bg-slate-100 px-2.5 py-1.5 text-[var(--color-heading)]"><ChatMarkdown text={m.content} /></div>
        </div>
      ) : m.status === "pending" ? (
        <p key={m.id} className="animate-pulse text-slate-400">Thinking…</p>
      ) : m.status === "error" ? (
        <p key={m.id} role="alert" className="text-red-600">{m.content}</p>
      ) : (
        <div key={m.id}>
          <ChatMarkdown text={m.content} />
          {view.onAddToNotes && (
            <button type="button" className="mt-1 inline-flex items-center gap-1 text-[11px] text-slate-400 hover:text-indigo-600"
              title="Queue a change that works this answer into the notes"
              onClick={() => view.onAddToNotes?.(thread, m.id)}>
              <NotebookPen className="h-3.5 w-3.5" /> Add to notes
            </button>
          )}
        </div>
      ))}
    </>
  );
}

export default function NotebookChat({
  threads, quotes, onRemoveQuote, onAsk, openThread, onOpenThread, onGoTo, onDelete, error, focusKey, imageUrl, onAddToNotes,
  onClear, docked, onGripDown,
}: Props) {
  const [text, setText] = useState("");
  const [fresh, setFresh] = useState(false); // "New topic": the next question starts a thread
  const [sending, setSending] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [confirmClear, setConfirmClear] = useState(false);
  const listRef = useRef<HTMLDivElement | null>(null);
  const boxRef = useRef<HTMLTextAreaElement | null>(null);
  const open = threads.find((t) => t.id === openThread) ?? null;
  const last = threads[threads.length - 1] ?? null;
  const multiDoc = threads.some((t) => t.anchors.some((a) => a.doc > 1));
  const view: View = { multiDoc, onGoTo, imageUrl, onAddToNotes };
  // Where the next question goes: quotes start a thread; otherwise it follows
  // up the open thread, or the latest one.
  const target = quotes.length || fresh ? null : (open ?? last);
  const waiting = target?.messages.some((m) => m.status === "pending") ?? false;

  const tail = threads.map((t) => `${t.id}:${t.messages.length}:${t.messages.at(-1)?.status}`).join(",");
  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [tail]);
  useEffect(() => { if (focusKey) boxRef.current?.focus(); }, [focusKey]);

  async function send() {
    const q = text.trim();
    if (!q || sending || waiting) return;
    setSending(true);
    try {
      await onAsk(q, target?.id ?? null);
      setText("");
      setFresh(false);
    } finally {
      setSending(false);
    }
  }


  return (
    <div className="notebook-chat flex h-full min-h-0 flex-col">
      {open && docked === "top" && (
        <OpenThread thread={open} view={view} onClose={() => onOpenThread(null)} onGripDown={onGripDown} />
      )}
      <div ref={listRef} className={`min-h-0 flex-1 space-y-4 overflow-auto px-3 py-3 ${TEXT_SIZE}`}>
        {threads.length === 0 && (
          <div className="space-y-2 pt-6 text-center text-sm text-slate-500">
            <p>Select text in the source or the notes and press Ask, or just type a question.</p>
            <p className="text-xs text-slate-400">Answers use the source and the notes. Quoted passages get a number you can come back to.</p>
          </div>
        )}
        {threads.map((t) => (
          <article key={t.id} className={`group space-y-2 border-l-2 pl-2.5 ${t.id === openThread ? "border-indigo-500" : target?.id === t.id ? "border-slate-300" : "border-transparent"}`}>
            <div className="flex items-center gap-2">
              {t.number > 0 && <Badge n={t.number} onClick={() => onOpenThread(t.id)} />}
              <span className="flex-1" />
              {confirmDelete === t.id ? (
                <span className="flex items-center gap-1.5 text-[11px]">
                  <button type="button" className="text-red-600 hover:underline" onClick={() => { setConfirmDelete(null); onDelete(t.id); }}>
                    Delete conversation
                  </button>
                  <button type="button" className="text-slate-500 hover:underline" onClick={() => setConfirmDelete(null)}>Keep</button>
                </span>
              ) : (
                <button type="button" className="p-0.5 text-slate-300 hover:text-red-600"
                  aria-label="Delete this conversation" title="Delete this conversation" onClick={() => setConfirmDelete(t.id)}>
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              )}
            </div>
            <Messages thread={t} {...view} />
          </article>
        ))}
      </div>
      {open && docked === "bottom" && (
        <OpenThread thread={open} view={view} bottom onClose={() => onOpenThread(null)} onGripDown={onGripDown} />
      )}
      <form className="shrink-0 space-y-2 border-t border-[var(--color-border)] p-2.5"
        onSubmit={(e) => { e.preventDefault(); void send(); }}>
        {quotes.length > 0 && (
          <ul className="space-y-1">
            {quotes.map((q, i) => (
              <li key={i} className="flex items-start gap-1.5 bg-indigo-50 px-2 py-1 text-xs text-slate-600">
                <span className="shrink-0 font-sans text-indigo-600">{where(q, q.doc > 1 || multiDoc)}</span>
                <span className="line-clamp-2 min-w-0 flex-1">{q.area ? "The selected box goes to the model as an image" : q.text}</span>
                <button type="button" className="shrink-0 text-slate-400 hover:text-red-600" aria-label="Remove quote"
                  onClick={() => onRemoveQuote(i)}>
                  <X className="h-3.5 w-3.5" />
                </button>
              </li>
            ))}
          </ul>
        )}
        <div className="flex items-center gap-2 text-[11px] text-slate-400">
          <span className="min-w-0 flex-1 truncate">
            {waiting
              ? "Waiting for the answer; you can write the next question meanwhile"
              : quotes.length
              ? "New question on the quoted text"
              : target
                ? `Follow-up${target.number ? ` in ${target.number}` : " to the last question"}`
                : "New question"}
          </span>
          {confirmClear ? (
            <span className="flex items-center gap-1.5">
              <button type="button" className="text-red-600 hover:underline"
                onClick={() => { setConfirmClear(false); setFresh(false); void onClear(); }}>
                Clear {threads.length} {threads.length === 1 ? "conversation" : "conversations"}
              </button>
              <button type="button" className="hover:text-slate-700 hover:underline" onClick={() => setConfirmClear(false)}>Keep</button>
            </span>
          ) : (
            <>
              {target && (
                <button type="button" className="inline-flex items-center gap-1 hover:text-slate-700" onClick={() => setFresh(true)}
                  title="Ask the next question apart from the earlier ones">
                  <MessageSquarePlus className="h-3.5 w-3.5" /> New topic
                </button>
              )}
              {threads.length > 0 && (
                <button type="button" className="inline-flex items-center gap-1 hover:text-slate-700" onClick={() => setConfirmClear(true)}
                  title="Clear this conversation and start again (numbers restart at 1)">
                  <Eraser className="h-3.5 w-3.5" /> New session
                </button>
              )}
            </>
          )}
        </div>
        {error && <p role="alert" className="text-xs text-red-600">{error}</p>}
        <div className="flex items-end gap-2">
          <textarea ref={boxRef} rows={2} value={text} className="input-field max-h-40 min-h-[2.75rem] flex-1 resize-y text-[13px]"
            style={{ padding: "0.5rem 0.65rem" }}
            placeholder={quotes.length ? "Ask about the quoted text…" : "Ask about the material…"}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                void send();
              }
            }} />
          <button type="submit" className="btn-primary h-9 w-9 shrink-0 rounded-full p-0" aria-label="Send"
            disabled={!text.trim() || sending || waiting} title={waiting ? "Waiting for the answer" : "Send (Enter)"}>
            <ArrowUp className="h-4 w-4" />
          </button>
        </div>
      </form>
    </div>
  );
}
