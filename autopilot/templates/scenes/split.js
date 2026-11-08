// "split": one wide frame (the long video) is sliced in three, and the pieces
// stand up as three vertical clips. The dot hops across them.
window.SCENE_FACTORY = function (spec, K) {
  const { el, set, P, lerp, ease, hop, squash } = K;
  const WX = 120, WY = 800, WW = 840, WH = 472;
  const TW = 230, TH = 409, GAP = 50, TY = 820;
  const TX = [540 - TW * 1.5 - GAP, 540 - TW / 2, 540 + TW / 2 + GAP];
  const frames = [0, 1, 2].map(() => el('rect', { class: 'hair', fill: 'none', rx: 4, opacity: 0 }));
  const outline = el('rect', { x: WX, y: WY, width: WW, height: WH, rx: 4, class: 'hair', fill: 'none',
                               'stroke-dasharray': 2624, 'stroke-dashoffset': 2624 });
  const slices = [1, 2].map(k => el('line', { x1: WX + WW * k / 3, x2: WX + WW * k / 3, class: 'hair', opacity: 0 }));
  const caps = [0, 1, 2].map(() => el('line', { class: 'hair thin', opacity: 0 }));
  const tops = TX.map(x => [x + TW / 2, TY - 24]);

  return {
    body: 5.6,
    swapAt: 2.6,
    cues: [0.25, 1.45, 1.7, 2.5, 3.3, 3.75, 4.2],
    render(t) {
      set(outline, { 'stroke-dashoffset': 2624 * (1 - P(t, .1, .9, ease.inOut3)) });
      slices.forEach((s, k) => {
        const p = P(t, 1.45 + k * .25, .3, ease.out3);
        set(s, { y1: WY - 30, y2: WY - 30 + (WH + 60) * p, opacity: p ? 1 : 0 });
      });
      // pieces separate and stand up into 9:16 frames
      const m = P(t, 2.3, .9, ease.inOut3);
      const on = t >= 2.3;
      set(outline, { opacity: on ? 0 : 1 });
      slices.forEach(s => set(s, { opacity: on ? 0 : +s.getAttribute('opacity') }));
      frames.forEach((f, k) => {
        const sx = WX + WW * k / 3, sw = WW / 3;
        set(f, { x: lerp(sx, TX[k], m), y: lerp(WY, TY, m), width: lerp(sw, TW, m), height: lerp(WH, TH, m), opacity: on ? 1 : 0 });
      });
      caps.forEach((c, k) => {
        const p = P(t, 3.4 + k * .45, .3);
        set(c, { x1: TX[k] + 50, x2: TX[k] + 50 + (TW - 100) * p, y1: TY + TH - 90, y2: TY + TH - 90, opacity: p ? .85 : 0 });
      });
      // the dot: sits in the wide frame, then hops onto each new clip
      let x = 540, y = WY + WH / 2, land = -1;
      if (t >= 2.3) { x = lerp(540, tops[0][0], m); y = lerp(WY + WH / 2, tops[0][1], m); }
      for (let k = 1; k < 3; k++) {
        const s = 3.3 + (k - 1) * .45;
        if (t >= s) { [x, y] = hop(t, s, .4, tops[k - 1][0], tops[k - 1][1], tops[k][0], tops[k][1], 120); land = s + .4; }
      }
      const [sx, sy] = squash(t, land);
      return [x, y, 22 * P(t, .2, .35, ease.backOut), sx, sy];
    },
  };
};
