// Копирует картинки, демо и отчёт из репозитория в public/media, чтобы не дублировать их в git.
import { cpSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const site = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repo = resolve(site, "..");
const out = resolve(site, "public/media");
mkdirSync(out, { recursive: true });

const files = {
  "docs/demo/demo.gif": "demo.gif",
  "docs/demo/demo.mp4": "demo.mp4",
  "docs/img/pipeline.svg": "pipeline.svg",
  "reports/v1.0/report.pdf": "report.pdf",
};
for (const [from, to] of Object.entries(files)) {
  cpSync(resolve(repo, from), resolve(out, to));
}
console.log(`media: ${Object.keys(files).length} файла скопированы в public/media`);
