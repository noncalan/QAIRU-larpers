// Рендер схемы mermaid в SVG и PNG для слайдов и README (issue #45).
//
// Запуск (нужны Node 18+, пакеты mermaid и playwright с установленным Chromium):
//   npm install --prefix /tmp/mmd mermaid@11.4.1 playwright
//   NODE_PATH=/tmp/mmd/node_modules node scripts/render_diagram.mjs docs/img/pipeline.mmd docs/img/pipeline
// Пишет docs/img/pipeline.svg и docs/img/pipeline.png. Сеть во время рендера не нужна:
// mermaid подключается из локального пакета, шрифт берётся из системы.

import { readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const [source, outBase] = process.argv.slice(2);
if (!source || !outBase) {
  console.error("Использование: node scripts/render_diagram.mjs СХЕМА.mmd ВЫХОД_БЕЗ_РАСШИРЕНИЯ");
  process.exit(2);
}

const mermaidJs = require.resolve("mermaid/dist/mermaid.min.js");
const definition = readFileSync(source, "utf8");
const FONT = "'Liberation Sans', 'DejaVu Sans', Arial, sans-serif";

const browser = await chromium.launch();
try {
  const page = await browser.newPage({ deviceScaleFactor: 2 });
  await page.setContent(`<!doctype html><html><body style="margin:0;background:#ffffff"><div id="out"></div></body></html>`);
  await page.addScriptTag({ path: mermaidJs });
  const svg = await page.evaluate(
    async ({ definition, font }) => {
      // eslint-disable-next-line no-undef
      mermaid.initialize({
        startOnLoad: false,
        theme: "base",
        securityLevel: "strict",
        flowchart: { htmlLabels: true, curve: "basis", nodeSpacing: 40, rankSpacing: 46, wrappingWidth: 300 },
        themeVariables: { fontFamily: font, fontSize: "15px", lineColor: "#555555", edgeLabelBackground: "#ffffff" },
      });
      // eslint-disable-next-line no-undef
      const { svg } = await mermaid.render("pipeline", definition);
      document.getElementById("out").innerHTML = svg;
      // Строка mermaid это HTML (<br> без закрытия): как файл .svg и в <img> на GitHub она не открывается.
      // XMLSerializer даёт корректный XML.
      return new XMLSerializer().serializeToString(document.querySelector("#out svg"));
    },
    { definition, font: FONT },
  );
  writeFileSync(`${outBase}.svg`, svg);
  const element = await page.$("#out svg");
  await element.screenshot({ path: `${outBase}.png`, omitBackground: false });
  console.log(`Записано: ${path.normalize(outBase)}.svg, ${path.normalize(outBase)}.png`);
} finally {
  await browser.close();
}
