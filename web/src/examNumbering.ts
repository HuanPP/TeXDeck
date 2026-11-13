// Section numbers are positions: after a reorder, a delete or an add, each
// heading's leading number is rewritten for its place, in the style the
// headings already use ("一、", "1.", "I.", "A.", "Part 2"). When some heading
// does not follow that style (the reader wrote their own), nothing is touched.
const ZH = "一二三四五六七八九";
const zhNumber = (n: number) => (n < 10 ? ZH[n - 1] : `${n >= 20 ? ZH[Math.floor(n / 10) - 1] : ""}十${n % 10 ? ZH[(n % 10) - 1] : ""}`);
const ROMAN = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII", "XIII", "XIV", "XV"];
type Scheme = { re: RegExp; make: (n: number, m: RegExpMatchArray) => string };
const SCHEMES: Scheme[] = [
  { re: /^([一二三四五六七八九十]+)(\s*[、．.]\s*)/u, make: (n, m) => zhNumber(n) + m[2] },
  { re: /^(part|section)(\s+)(\d+|[ivx]+|[a-h])\b(\s*[.:：、]?\s*)/i, make: (n, m) => m[1] + m[2]
    + (/^\d+$/.test(m[3]) ? String(n) : /^[ivx]+$/i.test(m[3]) ? ROMAN[n - 1] ?? String(n) : "ABCDEFGH"[n - 1] ?? String(n)) + m[4] },
  { re: /^(\d+)(\s*[.、．)）]\s*)/, make: (n, m) => String(n) + m[2] },
  { re: /^([IVX]+)(\s*[.、)）]\s*)/, make: (n, m) => (ROMAN[n - 1] ?? String(n)) + m[2] },
  { re: /^([A-H])(\s*[.、)）]\s*)/, make: (n, m) => ("ABCDEFGH"[n - 1] ?? String(n)) + m[2] },
];

export function renumber<T extends { title: string }>(sections: T[]): T[] {
  const named = sections.filter((s) => s.title.trim());
  const scheme = SCHEMES.find((sc) => named.length > 0 && named.every((s) => sc.re.test(s.title)));
  if (!scheme) return sections;
  const sample = named[0].title.match(scheme.re)!;
  return sections.map((s, i) => {
    const m = s.title.match(scheme.re);
    // A new, empty heading takes the number in the first heading's style.
    if (!m) return { ...s, title: scheme.make(i + 1, sample) };
    return { ...s, title: scheme.make(i + 1, m) + s.title.slice(m[0].length) };
  });
}
