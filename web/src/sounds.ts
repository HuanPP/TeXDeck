// Short synthesised sounds for learning moments (no audio files). Muting is
// remembered in localStorage.

const MUTE_KEY = "texdeck.sound.muted";
let ctx: AudioContext | null = null;

export function soundMuted(): boolean {
  return localStorage.getItem(MUTE_KEY) === "1";
}

export function setSoundMuted(muted: boolean) {
  localStorage.setItem(MUTE_KEY, muted ? "1" : "0");
}

type Note = { freq: number; at: number; length: number; type?: OscillatorType; gain?: number };

function play(notes: Note[]) {
  if (soundMuted()) return;
  try {
    ctx ??= new AudioContext();
    if (ctx.state === "suspended") void ctx.resume();
    const now = ctx.currentTime + 0.01;
    for (const n of notes) {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = n.type ?? "sine";
      osc.frequency.value = n.freq;
      const start = now + n.at;
      const peak = n.gain ?? 0.18;
      gain.gain.setValueAtTime(0.0001, start);
      gain.gain.exponentialRampToValueAtTime(peak, start + 0.015);
      gain.gain.exponentialRampToValueAtTime(0.0001, start + n.length);
      osc.connect(gain).connect(ctx.destination);
      osc.start(start);
      osc.stop(start + n.length + 0.02);
    }
  } catch {
    // No audio here (old browser, blocked): stay silent.
  }
}

// A part marked as learnt, an answer right: two rising notes.
export const playDone = () => play([
  { freq: 659.25, at: 0, length: 0.14, type: "triangle" },
  { freq: 987.77, at: 0.08, length: 0.28, type: "triangle" },
]);

// An answer wrong: a low, short buzz.
export const playWrong = () => play([
  { freq: 220, at: 0, length: 0.16, type: "square", gain: 0.06 },
  { freq: 185, at: 0.12, length: 0.24, type: "square", gain: 0.06 },
]);

// Everything done: an arpeggio up to a held chord.
export const playFinish = () => play([
  { freq: 523.25, at: 0, length: 0.16, type: "triangle" },
  { freq: 659.25, at: 0.1, length: 0.16, type: "triangle" },
  { freq: 783.99, at: 0.2, length: 0.16, type: "triangle" },
  { freq: 1046.5, at: 0.3, length: 0.6, type: "triangle" },
  { freq: 783.99, at: 0.3, length: 0.6, type: "sine", gain: 0.1 },
  { freq: 659.25, at: 0.3, length: 0.6, type: "sine", gain: 0.1 },
]);
