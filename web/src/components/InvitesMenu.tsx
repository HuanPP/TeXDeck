import { useEffect, useRef, useState } from "react";
import { ChevronDown } from "lucide-react";
import { createInvite, listInvites, revokeInvite, type Invite } from "../api";

const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));

// Admin only: one-use invite codes for signing up to this TeXDeck.
export default function InvitesMenu() {
  const [open, setOpen] = useState(false);
  const [invites, setInvites] = useState<Invite[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState("");
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    listInvites().then(setInvites, (e) => setError(errMsg(e)));
    const onDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  async function run(action: () => Promise<Invite[]>) {
    setError(null);
    try {
      setInvites(await action());
    } catch (e) {
      setError(errMsg(e));
    }
  }

  // The link opens the sign-up form with the code filled in.
  async function copy(code: string) {
    try {
      await navigator.clipboard.writeText(`${window.location.origin}/?invite=${encodeURIComponent(code)}`);
      setCopied(code);
    } catch {
      setCopied("");
    }
  }

  const unused = invites?.filter((i) => !i.used_by) ?? [];
  const used = invites?.filter((i) => i.used_by) ?? [];
  return (
    <div ref={rootRef} className="relative text-sm">
      <button type="button" className="btn-secondary px-3 py-1 text-xs" aria-expanded={open}
        onClick={() => setOpen((o) => !o)}>
        Invites
        <ChevronDown size={14} />
      </button>
      {open && (
        <div className="absolute right-0 top-full z-20 mt-2 w-[22rem] rounded-md border border-[var(--color-border)] bg-[var(--color-surface-container)] p-4 shadow-lg">
          <div className="flex items-center justify-between gap-3">
            <p className="text-xs text-slate-500">Each code signs up one account. New accounts connect their own models.</p>
            <button type="button" className="btn-primary shrink-0 px-3 py-1 text-xs" onClick={() => void run(createInvite)}>
              New code
            </button>
          </div>
          {error && <p role="alert" className="mt-2 text-xs text-red-700">{error}</p>}
          {invites === null && !error && <p className="mt-3 text-xs text-slate-500">Loading…</p>}
          {invites !== null && unused.length === 0 && (
            <p className="mt-3 text-xs text-slate-500">No unused codes.</p>
          )}
          <ul className="mt-3 space-y-2">
            {unused.map((i) => (
              <li key={i.code} className="flex items-center justify-between gap-2">
                <code className="truncate font-mono text-xs text-[var(--color-heading)]">{i.code}</code>
                <span className="flex shrink-0 gap-2 text-xs">
                  <button type="button" className="text-[var(--color-primary)] hover:underline" onClick={() => void copy(i.code)}>
                    {copied === i.code ? "Copied" : "Copy link"}
                  </button>
                  <button type="button" className="text-slate-500 hover:underline"
                    onClick={() => void run(() => revokeInvite(i.code))}>
                    Delete
                  </button>
                </span>
              </li>
            ))}
          </ul>
          {used.length > 0 && (
            <div className="mt-3 border-t border-[var(--color-border)] pt-3">
              <p className="text-xs text-slate-500">Used</p>
              <ul className="mt-1 space-y-1">
                {used.map((i) => (
                  <li key={i.code} className="flex justify-between gap-2 text-xs text-slate-500">
                    <code className="truncate font-mono">{i.code}</code>
                    <span className="shrink-0">{i.used_by}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
