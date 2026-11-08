// "growth": a quiet line graph climbs while one number counts up — posts per
// week going from 1 to 7 (one long video becomes a week of clips). The dot
// rides the tip of the line and lands on the top. Best in the "gold" theme.
window.SCENE_FACTORY = function (spec, K) {
  const { el, set, P, lerp, ease, squash } = K;
  const X0 = 150, X1 = 900, Y0 = 1300, Y1 = 760;
  // a gently compounding curve with a little texture, sampled densely
  const N = 120;
  const pts = [...Array(N + 1)].map((_, i) => {
    const u = i / N;
    const g = (Math.exp(2.4 * u) - 1) / (Math.exp(2.4) - 1);
    const wobble = Math.sin(u * 23) * 10 * (1 - u) + Math.sin(u * 61) * 4;
    return [lerp(X0, X1, u), lerp(Y0, Y1, g) + wobble];
  });
  const d = 'M' + pts.map(p => p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(' L');
  const axis = el('line', { x1: X0, y1: Y0 + 40, x2: X0, y2: Y0 + 40, class: 'hair thin', opacity: .45 });
  const path = el('path', { d, class: 'hair' });
  const len = path.getTotalLength();
  set(path, { 'stroke-dasharray': len, 'stroke-dashoffset': len });
  const ticks = [...Array(7)].map((_, k) => el('line', { x1: X0 + k * (X1 - X0) / 6, x2: X0 + k * (X1 - X0) / 6,
                                                       y1: Y0 + 34, y2: Y0 + 46, class: 'hair thin', opacity: 0 }));
  const num = document.createElement('div');
  num.style.cssText = "position:absolute;left:0;width:1080px;top:610px;text-align:center;font:400 150px/1 'Outfit';color:var(--fg);letter-spacing:-.03em;font-variant-numeric:tabular-nums";
  const unit = document.createElement('div');
  unit.style.cssText = "position:absolute;left:0;width:1080px;top:775px;text-align:center;font:400 36px/1 'Outfit';color:var(--soft)";
  unit.textContent = spec.unit || 'posts a week';
  const holder = document.getElementById('art-scene-html') || document.getElementById('root');
  holder.appendChild(num); holder.appendChild(unit);
  const ring = el('circle', { cx: X1, cy: Y1, r: 10, class: 'hair', fill: 'none', opacity: 0 });
  const from = spec.from || 1, to = spec.to || 7;

  return {
    body: 5.6,
    swapAt: 3.4,
    cues: [0.25, 1.2, 1.9, 2.5, 3.0, 3.35, 3.7],
    render(t) {
      set(axis, { x2: X0 + (X1 - X0) * P(t, .05, .6, ease.inOut3) });
      ticks.forEach((tk, k) => set(tk, { opacity: .5 * P(t, .2 + k * .05, .25) }));
      const g = P(t, .4, 3.0, ease.inOut2);
      set(path, { 'stroke-dashoffset': len * (1 - g) });
      const tip = path.getPointAtLength(len * g);
      // the number steps up on whole values, each step a small settle
      const val = Math.max(from, Math.min(to, Math.floor(lerp(from, to + .999, P(t, .5, 3.0, ease.inOut2)))));
      num.textContent = String(val);
      const fade = 1 - P(t, 5.6, .45, ease.inOut2);
      num.style.opacity = P(t, .1, .3) * fade;
      unit.style.opacity = .8 * P(t, .3, .4) * fade;
      set(ring, { cx: X1, cy: Y1 - 28, r: 10 + 46 * P(t, 3.55, .6, ease.out2),
                  opacity: t > 3.55 ? .9 * (1 - P(t, 3.55, .6)) : 0 });
      let x = tip.x, y = tip.y - 26;
      if (t > 3.4) { const h = P(t, 3.4, .3, ease.out2); y = lerp(tip.y - 26, Y1 - 26 - 8 * Math.sin(h * Math.PI), h); }
      const [sx, sy] = squash(t, 3.7);
      return [x, y, 22 * P(t, .3, .35, ease.backOut), sx, sy];
    },
  };
};
