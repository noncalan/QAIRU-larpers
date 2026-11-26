# Картинки для документации и слайдов

| Файл | Что это | Откуда |
|---|---|---|
| `pipeline.mmd` | Схема конвейера агента (исходник mermaid) | правится руками |
| `pipeline.svg` | Схема для README и `docs/architecture.md` | собирается из `pipeline.mmd` |
| `pipeline.png` | Та же схема для слайдов, 2560 px в ширину | собирается из `pipeline.mmd` |

Как пересобрать после правки `pipeline.mmd` (нужны Node 18+ и Chromium, который ставит Playwright):

```bash
npm install --prefix /tmp/mmd mermaid@11.4.1 playwright
NODE_PATH=/tmp/mmd/node_modules node scripts/render_diagram.mjs docs/img/pipeline.mmd docs/img/pipeline
```

Тот же текст схемы (без блока «Обозначения») стоит в `docs/architecture.md` в блоке mermaid: после правки исходника скопируйте его туда, чтобы картинка и текст не разошлись.
