import { useCallback, useEffect, useRef, useState } from "react";

// In-page replacement for window.prompt / window.confirm:
//   const [ask, dialog] = useAsk();  ...  {dialog}
//   const name = await ask({ title: "New file", input: "chapters/" });   // string | null
//   const ok = await ask({ title: "Delete x?", confirm: "Delete", danger: true });  // "" | null

type Request = {
  title: string;
  message?: string;
  input?: string; // present: ask for text, starting with this value
  placeholder?: string;
  confirm?: string; // the OK button's label
  danger?: boolean;
};

export function useAsk(): [(r: Request) => Promise<string | null>, React.ReactNode] {
  const [request, setRequest] = useState<(Request & { resolve: (v: string | null) => void }) | null>(null);
  const ask = useCallback((r: Request) => new Promise<string | null>((resolve) => setRequest({ ...r, resolve })), []);
  const close = (value: string | null) => {
    request?.resolve(value);
    setRequest(null);
  };
  return [ask, request ? <AskDialog key={request.title} request={request} onClose={close} /> : null];
}

function AskDialog({ request, onClose }: { request: Request; onClose: (value: string | null) => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [value, setValue] = useState(request.input ?? "");
  const asking = request.input !== undefined;
  useEffect(() => { dialog.current?.showModal(); }, []);
  return (
    <dialog ref={dialog} aria-labelledby="ask-title"
      className="m-auto w-[min(26rem,92vw)] border border-[var(--color-border)] p-0 shadow-xl backdrop:bg-slate-900/30"
      onCancel={(e) => { e.preventDefault(); onClose(null); }}>
      <form className="space-y-3 p-5" onSubmit={(e) => {
        e.preventDefault();
        if (!asking || value.trim()) onClose(asking ? value.trim() : "");
      }}>
        <h2 id="ask-title" className="text-base text-[var(--color-heading)]">{request.title}</h2>
        {request.message && <p className="text-sm text-slate-600">{request.message}</p>}
        {asking && (
          <input autoFocus value={value} placeholder={request.placeholder} aria-label={request.title}
            onChange={(e) => setValue(e.target.value)} className="input-field font-mono text-sm"
            onFocus={(e) => {
              // Select the file name, not the folder or extension, like a file manager.
              const v = e.currentTarget.value;
              const start = v.lastIndexOf("/") + 1;
              const dot = v.lastIndexOf(".");
              e.currentTarget.setSelectionRange(start, dot > start ? dot : v.length);
            }} />
        )}
        <div className="flex justify-end gap-2 pt-1">
          <button type="button" className="btn-secondary px-3 py-1.5 text-sm" onClick={() => onClose(null)}>Cancel</button>
          <button type="submit" autoFocus={!asking} disabled={asking && !value.trim()}
            className={`btn-primary px-3 py-1.5 text-sm ${request.danger ? "!bg-red-600 hover:!bg-red-700" : ""}`}>
            {request.confirm ?? "OK"}
          </button>
        </div>
      </form>
    </dialog>
  );
}
