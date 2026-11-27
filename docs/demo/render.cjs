// Рендер записи asciinema (docs/demo/demo.cast) в кадры PNG и список для ffmpeg.
// Нужны: Node 18+, пакеты playwright и @xterm/xterm@5.5.0, браузер Chromium для Playwright.
// Команды целиком: docs/demo/README.md.
//
//   node docs/demo/render.cjs docs/demo/demo.cast <папка кадров> <путь к node_modules/@xterm/xterm>
//
// Кадры снимаются только когда экран изменился; длительность кадра пишется в <папка>/frames.txt
// (формат concat-демультиплексора ffmpeg), поэтому паузы не раздувают число кадров.
'use strict';
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { chromium } = require('playwright');

const [castPath, outDir, xtermDir] = process.argv.slice(2);
if (!castPath || !outDir || !xtermDir) {
  console.error('usage: node render.cjs demo.cast out-dir node_modules/@xterm/xterm');
  process.exit(2);
}

const FPS = 12;
const TAIL = 4; // секунд на последнем кадре
const lines = fs.readFileSync(castPath, 'utf8').split('\n').filter(Boolean);
const header = JSON.parse(lines[0]);
const events = lines.slice(1).map((l) => JSON.parse(l)).filter((e) => e[1] === 'o');

const html = `<!doctype html><html><head><meta charset="utf-8">
<style>${fs.readFileSync(path.join(xtermDir, 'css/xterm.css'), 'utf8')}
html,body{margin:0;background:#0f1b2d}
#wrap{display:inline-block;padding:18px 22px;background:#0f1b2d}
.xterm-cursor-layer, .xterm-helper-textarea{display:none}
</style></head><body><div id="wrap"><div id="t"></div></div>
<script>${fs.readFileSync(path.join(xtermDir, 'lib/xterm.js'), 'utf8')}</script>
<script>
const term = new Terminal({cols:${header.width}, rows:${header.height}, fontSize:17, lineHeight:1.15,
  fontFamily:"'DejaVu Sans Mono','JetBrains Mono','Noto Color Emoji',monospace",
  cursorBlink:false, cursorStyle:'block', allowProposedApi:true,
  theme:{background:'#0f1b2d', foreground:'#e8e6df', cursor:'#e8e6df',
         brightBlack:'#8a94a6', green:'#4fbf7f', brightGreen:'#6fd39a'}});
term.open(document.getElementById('t'));
window.feed = (d) => new Promise((r) => term.write(d, r));
</script></body></html>`;

(async () => {
  fs.mkdirSync(outDir, { recursive: true });
  const browser = await chromium.launch();
  const page = await browser.newPage({ deviceScaleFactor: 1 });
  await page.setContent(html);
  await page.evaluate(() => document.fonts.ready);
  const wrap = await page.$('#wrap');

  const frames = []; // [file, start]
  let prevHash = '';
  let i = 0;
  const end = events[events.length - 1][0];
  for (let t = 0; i < events.length || t <= end; t += 1 / FPS) {
    let chunk = '';
    while (i < events.length && events[i][0] <= t) chunk += events[i++][2];
    if (!chunk) continue;
    await page.evaluate((d) => window.feed(d), chunk);
    const png = await wrap.screenshot();
    const hash = crypto.createHash('sha1').update(png).digest('hex');
    if (hash === prevHash) continue;
    prevHash = hash;
    const file = `f${String(frames.length).padStart(5, '0')}.png`;
    fs.writeFileSync(path.join(outDir, file), png);
    frames.push([file, t]);
  }
  await browser.close();

  let list = '';
  frames.forEach(([file, start], k) => {
    const next = k + 1 < frames.length ? frames[k + 1][1] : start + TAIL;
    list += `file '${file}'\nduration ${(next - start).toFixed(3)}\n`;
  });
  list += `file '${frames[frames.length - 1][0]}'\n`; // ffmpeg берёт длительность последнего так
  fs.writeFileSync(path.join(outDir, 'frames.txt'), list);
  console.log(`${frames.length} кадров, ${(frames[frames.length - 1][1] + TAIL).toFixed(1)} с`);
})();
