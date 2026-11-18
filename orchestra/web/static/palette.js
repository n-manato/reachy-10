/* One hue, shared by the scene and the console.
 *
 * The sections of the piece move the colour: cool for the verses, magenta for
 * the choruses, gold through the finale, and a different stop on the wheel the
 * second time round so the repeat does not look like a repeat. The stage reads
 * it for every emissive surface and the console reads the same number through
 * `--hue`, so the whole screen drifts with the music instead of fighting it.
 */

export const SECTION_HUE = {
  intro: 215, verse: 190, chorus: 300, finale: 45, outro: 230,
};

export const HUE_EASE = 2.4;   // seconds to travel to a new hue

export function hueFor(t, sections) {
  if (t === null) return SECTION_HUE.intro;
  let block = 0;
  for (const s of sections) {
    const isBlock = s.name === 'verse' || s.name === 'chorus';
    if (t >= s.start && t < s.end) {
      const base = SECTION_HUE[s.name] ?? SECTION_HUE.verse;
      if (!isBlock) return base;
      /* second lap: verse goes teal, chorus goes violet */
      const lap = Math.floor(block / 2);
      return (base + lap * (s.name === 'verse' ? -32 : -38) + 720) % 360;
    }
    if (isBlock) block++;
  }
  return SECTION_HUE.outro;
}

/** One frame of easing toward `target`, the short way round the wheel. */
export function stepHue(current, target, dt) {
  const d = ((target - current + 540) % 360) - 180;
  return (current + d * Math.min(1, dt / HUE_EASE) + 360) % 360;
}

export const NOTE_NAMES = ['C', 'C♯', 'D', 'D♯', 'E', 'F', 'F♯', 'G', 'G♯', 'A', 'A♯', 'B'];

export function noteName(pitch) {
  return `${NOTE_NAMES[((pitch % 12) + 12) % 12]}${Math.floor(pitch / 12) - 1}`;
}
