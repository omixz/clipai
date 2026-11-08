// "waveform": an audio waveform of thin bars; the dot scans it with a playhead,
// brackets snap around the loudest stretch, everything else goes quiet.
window.SCENE_FACTORY = function (spec, K) {
  const { el, set, P, lerp, ease, rand, squash } = K;
  const r = rand(11);
  const N = 46, X0 = 130, X1 = 950, MID = 1020, step = (X1 - X0) / (N - 1);
  const HOT0 = 27, HOT1 = 35;
  const heights = [...Array(N)].map((_, i) => {
    const base = 30 + r() * 90;
    return i >= HOT0 && i <= HOT1 ? 190 + r() * 150 : base;
  });
  const bars = heights.map((h, i) => el('line', { x1: X0 + i * step, x2: X0 + i * step, y1: MID, y2: MID, class: 'hair' }));
  const head = el('line', { y1: MID - 260, y2: MID + 260, class: 'hair thin', opacity: 0 });
  const bx0 = X0 + HOT0 * step - 26, bx1 = X0 + HOT1 * step + 26, by0 = MID - 230, by1 = MID + 230;
  const corner = (x, y, dx, dy) => el('path', { d: `M${x + dx * 40} ${y} L${x} ${y} L${x} ${y + dy * 40}`, class: 'hair', fill: 'none', opacity: 0 });
  const corners = [corner(bx0, by0, 1, 1), corner(bx1, by0, -1, 1), corner(bx0, by1, 1, -1), corner(bx1, by1, -1, -1)];

  return {
    body: 5.2,
    swapAt: 2.95,
    cues: [0.3, 2.85, 3.0],
    render(t) {
      const sel = P(t, 2.85, .5, ease.inOut2);
      bars.forEach((b, i) => {
        const g = P(t, .1 + i * .018, .45, ease.out3);
        const breathe = 1 + .06 * Math.sin(t * 5 + i);
        const h = heights[i] * g * breathe / 2;
        const hot = i >= HOT0 && i <= HOT1;
        set(b, { y1: MID - h, y2: MID + h, opacity: hot ? 1 : 1 - .78 * sel });
      });
      // playhead + dot scan left to right, stopping on the loud part
      const scan = P(t, .9, 1.9, ease.inOut3);
      const stopX = X0 + ((HOT0 + HOT1) / 2) * step;
      const hx = lerp(X0, stopX, scan);
      set(head, { x1: hx, x2: hx, opacity: (t > .9 ? .5 : 0) * (1 - sel) });
      // brackets snap in from slightly outside
      corners.forEach((c, k) => {
        const p = P(t, 2.85 + k * .04, .3, ease.backOut);
        const ox = (k % 2 ? 1 : -1) * 30 * (1 - p), oy = (k < 2 ? -1 : 1) * 30 * (1 - p);
        set(c, { opacity: p, transform: `translate(${ox} ${oy})` });
      });
      // after selection, the dot hops up onto the bracket's top edge
      let x = hx, y = MID + 300;
      const up = P(t, 3.4, .5, ease.out3);
      if (t > 3.4) y = lerp(MID + 300, by0 - 26, up) - Math.sin(up * Math.PI) * 60;
      const [sx, sy] = squash(t, 3.9);
      return [x, y, 22 * P(t, .3, .35, ease.backOut), sx, sy];
    },
  };
};
