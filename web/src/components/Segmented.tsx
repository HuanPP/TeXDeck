// A one-of-several choice drawn as joined buttons, so it reads as a setting
// rather than as separate actions.
export default function Segmented<T extends string>(props: {
  label: string;
  value: T;
  options: { value: T; label: string }[];
  onChange: (value: T) => void;
  disabled?: boolean;
}) {
  return (
    <div role="radiogroup" aria-label={props.label}
      className="inline-flex overflow-hidden rounded border border-[var(--color-border)] bg-white text-sm">
      {props.options.map((o, i) => {
        const selected = o.value === props.value;
        return (
          <button key={o.value} type="button" role="radio" aria-checked={selected} disabled={props.disabled}
            onClick={() => props.onChange(o.value)}
            className={`px-3 py-1.5 disabled:opacity-50 ${i > 0 ? "border-l border-[var(--color-border)]" : ""} ${
              selected
                ? "bg-[var(--color-primary)] text-white"
                : "text-slate-600 hover:bg-[var(--color-surface)]"
            }`}>
            {o.label}
          </button>
        );
      })}
    </div>
  );
}
