// "credits": a grid of credit tokens quietly expire one by one; then a single
// dot remains with a calm ring of twelve ticks (months) drawn around it.
window.SCENE_FACTORY = function (spec, K) {
  const { el, set, P, lerp, ease, rand, squash } = K;
  const r = rand(5);
  const COLS = 5, ROWS = 4, GX = 300, GY = 860, S = 120;
  const cells = [];
  for (let j = 0; j < ROWS; j++) for (let i = 0; i < COLS; i++) {
    const cx = GX + i * S, cy = GY + j * S;
    cells.push({ ring: el('circle', { cx, cy, r: 0, class: 'hair', fill: 'none' }),
                 fill: el('circle', { cx, cy, r: 0, class: 'solid' }), order: r() });
  }
  const order = [...cells].sort((a, b) => a.order - b.order);
  const CX = 540, CY = 1040;
  const ticks = [...Array(12)].map((_, k) => {
    const a = k / 12 * Math.PI * 2 - Math.PI / 2;
    return el('line', { x1: CX + Math.cos(a) * 150, y1: CY + Math.sin(a) * 150,
                        x2: CX + Math.cos(a) * 172, y2: CY + Math.sin(a) * 172, class: 'hair', opacity: 0 });
  });

  return {
    body: 5.4,
    swapAt: 2.9,
    cues: [0.3, 3.0, 3.6],
    render(t) {
      const gone = P(t, 2.75, .5, ease.inOut2);
      cells.forEach((c, k) => {
        const p = P(t, .1 + k * .03, .35, ease.backOut);
        set(c.ring, { r: 13 * p, opacity: (1 - gone) * .7 });
        set(c.fill, { r: 9 * p, opacity: 1 - gone });
      });
      // expiring: each filled token empties, in a scattered order
      order.forEach((c, k) => {
        const e = P(t, 1.0 + k * .08, .25, ease.in2);
        set(c.fill, { r: 9 * P(t, .1, .35, ease.backOut) * (1 - e) });
      });
      // the calm alternative: one dot, a ring of months
      ticks.forEach((tk, k) => set(tk, { opacity: P(t, 3.6 + k * .06, .25) }));
      const d = P(t, 3.0, .5, ease.backOut);
      const [sx, sy] = squash(t, 3.0);
      return [CX, CY, t < 3.0 ? 0 : 26 * d, sx, sy];
    },
  };
};
