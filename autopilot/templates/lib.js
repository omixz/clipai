// Minimal motion kit for Peakcut reels.
// Style rules (after the "minimalistic-graphics" look): pure black (or white),
// hairline strokes, ONE solid dot, ONE quiet lowercase line of text.
// Every scene is a pure function of time — render(t) — so a frame is exactly
// reproducible and the renderer can step through it frame by frame.
(function () {
  const W = 1080, H = 1920, NS = 'http://www.w3.org/2000/svg';
  const clamp = (x, a = 0, b = 1) => Math.min(b, Math.max(a, x));
  const lerp = (a, b, p) => a + (b - a) * p;
  const ease = {
    linear: p => p,
    out2: p => 1 - (1 - p) ** 2,
    out3: p => 1 - (1 - p) ** 3,
    in2: p => p * p,
    inOut2: p => (p < .5 ? 2 * p * p : 1 - (-2 * p + 2) ** 2 / 2),
    inOut3: p => (p < .5 ? 4 * p ** 3 : 1 - (-2 * p + 2) ** 3 / 2),
    backOut: p => { const c1 = 2.2, c3 = c1 + 1; return 1 + c3 * (p - 1) ** 3 + c1 * (p - 1) ** 2; },
  };
  // eased progress of a tween that starts at `start` and lasts `dur`
  const P = (t, start, dur, e = ease.out3) => e(clamp((t - start) / Math.max(1e-6, dur)));
  // deterministic pseudo-random (same picture on every render)
  const rand = seed => () => ((seed = (seed * 16807) % 2147483647) / 2147483647);

  function el(tag, attrs = {}, parent) {
    const e = document.createElementNS(NS, tag);
    set(e, attrs);
    (parent || document.getElementById('art-scene')).appendChild(e);
    return e;
  }
  function set(e, attrs) {
    for (const k in attrs) e.setAttribute(k, typeof attrs[k] === 'number' ? +attrs[k].toFixed(2) : attrs[k]);
  }
  // a dot squash on landing: returns [sx, sy]
  function squash(t, at) {
    const d = t - at;
    if (d < 0 || d > .28) return [1, 1];
    const k = Math.sin((d / .28) * Math.PI) * (1 - d / .28);
    return [1 + .22 * k, 1 - .2 * k];
  }
  // a hop along x from (x0,y0) to (x1,y1) peaking `lift` px above the higher point
  function hop(t, start, dur, x0, y0, x1, y1, lift = 110) {
    const p = clamp((t - start) / dur);
    const x = lerp(x0, x1, p);
    const top = Math.min(y0, y1) - lift;
    const y = p < .5 ? lerp(y0, top, ease.out2(p * 2)) : lerp(top, y1, ease.in2((p - .5) * 2));
    return [x, y];
  }

  // ── caption: one line, a soft crossfade to the second ──
  function caption(lines, swapAt) {
    const a = document.getElementById('c1'), b = document.getElementById('c2');
    a.textContent = lines[0] || '';
    b.textContent = lines[1] || '';
    return t => {
      const intro = P(t, .15, .5, ease.out2);
      const sw = lines.length > 1 ? P(t, swapAt, .35, ease.inOut2) : 0;
      a.style.opacity = intro * (1 - sw);
      a.style.transform = `translateY(${(1 - intro) * 14 - sw * 10}px)`;
      b.style.opacity = sw;
      b.style.transform = `translateY(${(1 - sw) * 14}px)`;
    };
  }

  // ── end card: the scene fades, the dot glides in to become the full stop
  //    of the wordmark — "peakcut." — then "link in bio" appears. ──
  function endcard(start, cta) {
    const word = document.getElementById('word'), sub = document.getElementById('sub');
    sub.textContent = cta || 'link in bio';
    let px = null, py = null;
    function measure() {
      // the text itself, not its full-width container
      const r = document.getElementById('wordText').getBoundingClientRect();
      px = r.right + 16; py = r.top + r.height * .78;
    }
    return {
      start,
      duration: 2.1,
      // returns the dot override [x, y, r] or null before the end card starts
      render(t, from) {
        const scene = document.getElementById('art-scene'), cap = document.getElementById('caps');
        const f = P(t, start, .45, ease.inOut2);
        scene.style.opacity = 1 - f;
        cap.style.opacity = 1 - f;
        if (t < start) { word.style.opacity = 0; sub.style.opacity = 0; return null; }
        if (px === null) measure();
        const w = P(t, start + .4, .5, ease.out2);
        word.style.opacity = w;
        word.style.transform = `translateY(${(1 - w) * 12}px)`;
        const s = P(t, start + .85, .5, ease.out2);
        sub.style.opacity = s * .75;
        const m = P(t, start, .75, ease.inOut3);
        return [lerp(from[0], px, m), lerp(from[1], py, m), lerp(from[2] || 22, 10, m)];
      },
    };
  }

  window.Peakcut = { W, H, NS, clamp, lerp, ease, P, rand, el, set, squash, hop, caption, endcard };
})();
