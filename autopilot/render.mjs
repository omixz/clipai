// Frame-exact renderer: loads a built page, steps SCENE.render(t) through
// every frame and pipes screenshots into ffmpeg. Deterministic — the same
// spec always gives the same video — and needs no screen or GPU.
//
//   node render.mjs <page.html> <out.mp4> [fps]
// Prints the scene's {duration, cues} as JSON on the last line (for audio).
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';

const require = createRequire(import.meta.url);
let playwright;
try { playwright = require('playwright'); } catch {
  // fall back to a globally installed Playwright (e.g. preinstalled on the runner)
  const { execSync } = await import('node:child_process');
  const root = execSync('npm root -g').toString().trim();
  playwright = require(`${root}/playwright`);
}

const argv = process.argv.slice(2);
const metaOnly = argv.includes('--meta');
// --alpha: transparent PNG frames into a lossless .mov, for compositing over footage
const alpha = argv.includes('--alpha');
const [page, out, fpsArg] = argv.filter(a => a !== '--meta' && a !== '--alpha');
const fps = Number(fpsArg || 30);
if (!page || (!out && !metaOnly)) { console.error('usage: node render.mjs page.html out.mp4 [fps]'); process.exit(2); }

const browser = await playwright.chromium.launch();
try {
  const ctx = await browser.newContext({ viewport: { width: 1080, height: 1920 }, deviceScaleFactor: 1 });
  const p = await ctx.newPage();
  const errors = [];
  p.on('pageerror', e => errors.push(e.message));
  await p.goto(pathToFileURL(page).href);
  await p.evaluate(() => document.fonts.ready);
  if (errors.length) throw new Error('page error: ' + errors.join('; '));
  const meta = await p.evaluate(() => ({ duration: window.SCENE.duration, cues: window.SCENE.cues,
    sceneDuration: window.SCENE.sceneDuration, sceneCues: window.SCENE.sceneCues }));
  if (metaOnly) { console.log(JSON.stringify(meta)); process.exit(0); }
  const frames = Math.round(meta.duration * fps);

  const encode = alpha
    ? ['-c:v', 'png', '-i', '-', '-c:v', 'png', '-pix_fmt', 'rgba', out]
    : ['-c:v', 'mjpeg', '-i', '-', '-c:v', 'libx264', '-preset', 'medium', '-crf', '18', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', out];
  const ff = spawn('ffmpeg', ['-y', '-v', 'error', '-f', 'image2pipe', '-framerate', String(fps), ...encode],
    { stdio: ['pipe', 'inherit', 'inherit'] });
  const done = new Promise((res, rej) => ff.on('close', c => (c === 0 ? res() : rej(new Error('ffmpeg exited ' + c)))));

  for (let f = 0; f < frames; f++) {
    await p.evaluate(t => window.SCENE.render(t), f / fps);
    const buf = alpha ? await p.screenshot({ type: 'png', omitBackground: true })
                      : await p.screenshot({ type: 'jpeg', quality: 95 });
    if (!ff.stdin.write(buf)) await new Promise(r => ff.stdin.once('drain', r));
  }
  ff.stdin.end();
  await done;
  if (errors.length) throw new Error('page error: ' + errors.join('; '));
  console.log(JSON.stringify({ ...meta, frames, fps }));
} finally {
  await browser.close();
}
