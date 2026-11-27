# Лендинг QAIRU validator

Сайт: [qairu-validator.vercel.app](https://qairu-validator.vercel.app/). React + Vite, собирается в статику. Код агента не затрагивает. Цифры взяты из README и отчёта на сдачу [`reports/v1.0/`](../reports/v1.0/README.md); если они расходятся, верен README.

```bash
cd site
npm install
npm run dev      # http://127.0.0.1:5173/
npm run build    # статика в site/dist
```

- Тексты и цифры лежат в `src/content.js`, только из README, `docs/value.md`, `docs/architecture.md`, `docs/demo.md` и `reports/v1.0`.
- Картинки, демо и отчёт не дублируются в git: `scripts/copy-assets.mjs` копирует их из репозитория в `public/media/` перед `dev` и `build`.
- `base: "./"`, поэтому сборка работает из любой подпапки, в том числе `/QAIRU-larpers/` на Pages.
- Шрифты Onest и JetBrains Mono ставятся из npm (`@fontsource-variable`) и попадают в сборку: внешних CDN нет.
- Публикация: `.github/workflows/pages.yml` при push в `main`. Один раз включить Settings → Pages → Source: GitHub Actions.
- Vercel: https://qairu-validator.vercel.app. Обновить: `npx vercel@latest build --prod --yes && npx vercel@latest deploy --prebuilt --prod --yes` из `site/` (сборка локальная, потому что медиа берутся из корня репозитория).
