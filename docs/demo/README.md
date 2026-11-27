# Запись терминала для демо

Запись четырёх шагов демо (#55): проверка `requirements.pdf` одной командой, первая страница отчёта, битый файл и вопросы автору с повторной проверкой по ответам. Сценарий видео с текстом для голоса: [`docs/demo.md`](../demo.md).

![Запись терминала: проверка PDF, битый файл, вопросы автору](demo.gif)

**Модель здесь не вызывается.** Проверка документов идёт через `--backend replay` по записям живых прогонов ([`reports/v0.9-rc2/run`](../../reports/v0.9-rc2/run) для PDF, [`tests/fixtures/e2e/md/run`](../../tests/fixtures/e2e/md/run) для Markdown), а битый файл отсекается до любого вызова модели. Скрипт к тому же убирает `claude` из `PATH` и ключи API из окружения и отказывается работать, если `claude` всё ещё найден.

| Файл | Что это |
|---|---|
| [`demo.sh`](demo.sh) | Скрипт демо: «набирает» команды и выполняет их. Отчёты кладёт в `out/demo/` (папка в `.gitignore`) |
| [`demo.cast`](demo.cast) | Запись терминала asciinema 2.4 (110×34, 93 с). Смотреть: `asciinema play docs/demo/demo.cast` |
| [`demo.mp4`](demo.mp4) | Та же запись видео для монтажа и формы сдачи |
| [`demo.gif`](demo.gif) | Та же запись для README |
| [`render.cjs`](render.cjs) | Рендер `demo.cast` в кадры через xterm.js и Playwright, кадры склеивает ffmpeg |

## Посмотреть без записи

```bash
bash docs/demo/demo.sh             # с паузами, как на записи, около 1,5 минуты
DEMO_FAST=1 bash docs/demo/demo.sh # без пауз, секунды
```

## Перезаписать (например, после v1.0)

Нужны `uv`, Node 18+ с Playwright и Chromium, ffmpeg. agg для asciinema из PyPI не ставится, поэтому кадры рисует `render.cjs`.

```bash
# 1. Запись терминала
uv tool run --from asciinema==2.4.0 asciinema rec --overwrite --cols 110 --rows 34 \
  -t "QAIRU validator: демо (replay)" -c "bash docs/demo/demo.sh" docs/demo/demo.cast

# 2. Кадры (xterm.js во временной папке)
npm install --prefix /tmp/demo-render @xterm/xterm@5.5.0
node docs/demo/render.cjs docs/demo/demo.cast /tmp/demo-frames /tmp/demo-render/node_modules/@xterm/xterm

# 3. MP4 и GIF
cd /tmp/demo-frames
ffmpeg -y -f concat -safe 0 -i frames.txt -vf "fps=12,format=yuv420p,scale=trunc(iw/2)*2:trunc(ih/2)*2" \
  -c:v libx264 -crf 20 -movflags +faststart "$OLDPWD/docs/demo/demo.mp4"
ffmpeg -y -f concat -safe 0 -i frames.txt -vf "fps=8,scale=880:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=64:stats_mode=diff[p];[b][p]paletteuse=dither=none:diff_mode=rectangle" \
  "$OLDPWD/docs/demo/demo.gif"
```

Если релиз v1.0 положит новую запись прогона, поменяйте в `demo.sh` папку `--replay` в шаге 1 и цифры в [`docs/demo.md`](../demo.md).
