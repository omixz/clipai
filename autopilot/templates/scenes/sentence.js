// "sentence": words as hairline dashes ending in a full stop. A cut mark tries
// to land mid-sentence, shakes and fades; the dot hops word to word and the
// cut lands on the full stop instead.
window.SCENE_FACTORY = function (spec, K) {
  const { el, set, P, lerp, ease, rand, hop, squash } = K;
  const r = rand(7);
  const Y = 1040, words = [];
  let x = 160;
  while (x < 830) {
    const w = 46 + Math.round(r() * 70);
    if (x + w > 830) break;
    words.push([x, w]);
    x += w + 22;
  }
  const end = x + 2;
  const segs = words.map(([wx, w]) => el('line', { x1: wx, y1: Y, x2: wx, y2: Y, class: 'hair' }));
  const stop = el('circle', { cx: end, cy: Y - 2, r: 0, class: 'solid' });
  const midIdx = Math.floor(words.length * .45);
  const midX = words[midIdx][0] - 11;
  const bad = el('line', { x1: midX, y1: Y - 40, x2: midX, y2: Y + 40, class: 'hair', opacity: 0 });
  const good = el('line', { x1: end + 26, y1: Y - 40, x2: end + 26, y2: Y + 40, class: 'hair', opacity: 0 });
  const ring = el('circle', { cx: end, cy: Y - 2, r: 10, class: 'hair', fill: 'none', opacity: 0 });
  const hopDur = .22, startHop = 1.0;
  const tops = words.map(([wx, w]) => wx + w / 2);

  return {
    body: 5.3,
    swapAt: 3.05,
    cues: [0.3, 1.75, 2.2, 3.15, 3.35],
    render(t) {
      segs.forEach((s, i) => {
        const p = P(t, .1 + i * .045, .3);
        set(s, { x2: words[i][0] + words[i][1] * p });
      });
      set(stop, { r: 7 * P(t, .1 + words.length * .045, .3, ease.backOut) });
      // the wrong cut: drops in, shakes, fades
      const b = P(t, 1.75, .2, ease.backOut);
      const shake = t > 1.95 && t < 2.35 ? Math.sin((t - 1.95) * 70) * 7 * (1 - (t - 1.95) / .4) : 0;
      set(bad, { x1: midX + shake, x2: midX + shake, opacity: b * (1 - P(t, 2.25, .35)), y1: Y - 40 * b, y2: Y + 40 * b });
      // the right cut, on the full stop
      const g = P(t, 3.15, .25, ease.backOut);
      set(good, { opacity: g, y1: Y - 40 * g, y2: Y + 40 * g });
      set(ring, { r: 10 + 50 * P(t, 3.15, .6, ease.out2), opacity: .9 * (1 - P(t, 3.15, .6)) * (t > 3.15 ? 1 : 0) });
      // after the cut: everything settles, the sentence lifts slightly as "the clip"
      const lift = P(t, 3.5, .7, ease.inOut3) * 26;
      segs.forEach(s => set(s, { y1: Y - lift, y2: Y - lift }));
      set(stop, { cy: Y - 2 - lift });
      // the dot hops word to word (pausing where the bad cut appears)
      let dx = tops[0], dy = Y - 40, land = -1;
      for (let i = 1; i < tops.length; i++) {
        const s = startHop + (i - 1) * hopDur + (i > midIdx ? .45 : 0);
        if (t >= s) { [dx, dy] = hop(t, s, hopDur, tops[i - 1], Y - 40, tops[i], Y - 40, 60); land = s + hopDur; }
      }
      const sLast = startHop + (tops.length - 1) * hopDur + .45;
      if (t >= sLast) { [dx, dy] = hop(t, sLast, .3, tops[tops.length - 1], Y - 40, end + 58, Y - 40, 80); land = sLast + .3; }
      dy -= lift;
      const [sx, sy] = squash(t, land);
      return [dx, dy, 20 * P(t, .2, .35, ease.backOut), sx, sy];
    },
  };
};
