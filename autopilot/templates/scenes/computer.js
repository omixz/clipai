// "computer": a quiet monitor outline. The dot (your video) drops down a dashed
// line into the screen, a progress hairline fills, three small clips appear.
window.SCENE_FACTORY = function (spec, K) {
  const { el, set, P, lerp, ease, squash } = K;
  const SX = 220, SY = 820, SW = 640, SH = 400;
  const perim = 2 * (SW + SH);
  const screen = el('rect', { x: SX, y: SY, width: SW, height: SH, rx: 18, class: 'hair', fill: 'none',
                              'stroke-dasharray': perim, 'stroke-dashoffset': perim });
  const neck = el('line', { x1: 540, x2: 540, y1: SY + SH, y2: SY + SH, class: 'hair' });
  const base = el('line', { x1: 540, x2: 540, y1: SY + SH + 70, y2: SY + SH + 70, class: 'hair' });
  const drop = el('line', { x1: 540, x2: 540, y1: 640, y2: 640, class: 'hair thin', 'stroke-dasharray': '6 12', opacity: .6 });
  const bar = el('line', { x1: SX + 120, x2: SX + 120, y1: SY + SH - 70, y2: SY + SH - 70, class: 'hair' });
  const clips = [-1, 0, 1].map(k => el('rect', { x: 540 + k * 120 - 40, y: SY + 110, width: 80, height: 142, rx: 3, class: 'hair', fill: 'none', opacity: 0 }));

  return {
    body: 5.4,
    swapAt: 3.0,
    cues: [0.3, 1.9, 3.3, 3.55, 3.8],
    render(t) {
      set(screen, { 'stroke-dashoffset': perim * (1 - P(t, .1, 1.0, ease.inOut3)) });
      const n = P(t, .8, .4);
      set(neck, { y2: SY + SH + 70 * n });
      const b = P(t, 1.0, .4, ease.inOut2);
      set(base, { x1: 540 - 130 * b, x2: 540 + 130 * b });
      // dashed path from above, then the dot falls into the screen
      const dl = P(t, .9, .6, ease.out2);
      set(drop, { y2: 640 + (SY + 130 - 640) * dl, opacity: .6 * (1 - P(t, 2.3, .4)) });
      let x = 540, y = 620;
      const f = P(t, 1.4, .5, ease.in2);
      y = lerp(620, SY + SH / 2, f);
      // progress hairline: the computer working
      const w = P(t, 2.1, 1.0, ease.inOut2);
      set(bar, { x2: SX + 120 + (SW - 240) * w, opacity: 1 - P(t, 3.2, .3) });
      clips.forEach((c, k) => {
        const p = P(t, 3.3 + k * .25, .3, ease.backOut);
        set(c, { opacity: p, transform: `translate(0 ${(1 - p) * 20})` });
      });
      // the dot settles under the middle clip
      const s = P(t, 3.2, .5, ease.inOut3);
      if (t > 3.2) y = lerp(SY + SH / 2, SY + 300, s);
      const [sx, sy] = squash(t, 1.9);
      return [x, y, 22 * P(t, .2, .35, ease.backOut), sx, sy];
    },
  };
};
