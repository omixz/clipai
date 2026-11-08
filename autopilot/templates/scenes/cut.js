// "cut": a long timeline (the hour-long video); the dot travels along it, two
// cut marks drop, and the piece between them lifts and opens into a 9:16 frame.
window.SCENE_FACTORY = function (spec, K) {
  const { el, set, P, lerp, ease, squash } = K;
  const Y = 1040, X0 = 110, X1 = 970, CA = 560, CB = 700;
  const line = el('line', { x1: X0, y1: Y, x2: X1, y2: Y, class: 'hair', 'stroke-dasharray': 860, 'stroke-dashoffset': 860 });
  const ticks = [];
  for (let x = X0; x <= X1; x += 43) ticks.push(el('line', { x1: x, y1: Y + 18, x2: x, y2: Y + 30, class: 'hair thin', opacity: 0 }));
  const cutA = el('line', { x1: CA, y1: Y - 34, x2: CA, y2: Y + 34, class: 'hair', opacity: 0 });
  const cutB = el('line', { x1: CB, y1: Y - 34, x2: CB, y2: Y + 34, class: 'hair', opacity: 0 });
  const frame = el('rect', { class: 'hair', fill: 'none', opacity: 0, rx: 4 });
  const bars = [0, 1, 2].map(() => el('line', { class: 'hair thin', opacity: 0 }));
  const play = el('line', { class: 'hair', opacity: 0 });
  const FW = 300, FH = 533, FX = 540 - FW / 2, FY = 760;

  return {
    body: 5.4,
    swapAt: 3.25,
    cues: [0.25, 2.55, 2.75, 3.2, 4.1],
    render(t) {
      set(line, { 'stroke-dashoffset': 860 * (1 - P(t, .1, .8, ease.inOut3)) });
      ticks.forEach((k, i) => set(k, { opacity: .35 * P(t, .3 + i * .02, .3) * (1 - .6 * P(t, 3.0, .5)) }));
      // the dot travels the timeline, then dips to the middle of the cut
      let x = lerp(X0, 630, P(t, .7, 1.8, ease.inOut3)), y = Y - 22;
      const cutIn = P(t, 2.55, .25, ease.backOut), cutIn2 = P(t, 2.75, .25, ease.backOut);
      set(cutA, { opacity: cutIn, y1: Y - 34 * cutIn, y2: Y + 34 * cutIn });
      set(cutB, { opacity: cutIn2, y1: Y - 34 * cutIn2, y2: Y + 34 * cutIn2 });
      // the cut piece lifts and opens into a vertical frame
      const m = P(t, 3.1, .9, ease.inOut3);
      const fx = lerp(CA, FX, m), fw = lerp(CB - CA, FW, m), fy = lerp(Y - 1, FY, m), fh = lerp(2, FH, m);
      set(frame, { x: fx, y: fy, width: fw, height: fh, opacity: t > 3.05 ? 1 : 0 });
      set(line, { opacity: 1 - .75 * m });
      [cutA, cutB].forEach(c => set(c, { opacity: +c.getAttribute('opacity') * (1 - m) }));
      if (t >= 3.1) { x = lerp(630, 540, m); y = lerp(Y - 22, FY + FH - 70, m); }
      // inside the frame: three caption hairlines and a play bar fill
      bars.forEach((b, i) => {
        const p = P(t, 4.0 + i * .12, .35);
        const w = [170, 120, 150][i];
        set(b, { x1: 540 - w / 2, x2: 540 - w / 2 + w * p, y1: FY + 330 + i * 34, y2: FY + 330 + i * 34, opacity: p ? .8 : 0 });
      });
      const pb = P(t, 4.2, 1.1, ease.linear);
      set(play, { x1: FX + 24, x2: FX + 24 + (FW - 48) * pb, y1: FY + FH - 30, y2: FY + FH - 30, opacity: pb ? 1 : 0 });
      const pop = P(t, .2, .35, ease.backOut);
      const [sx, sy] = squash(t, 4.0);
      return [x, y, 22 * pop, sx, sy];
    },
  };
};
