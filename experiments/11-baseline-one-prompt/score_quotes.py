"""Проверка дословности цитат в ответах baseline (эксперимент 11).

Берёт три ответа experiments/out/11/baseline_run{1,2,3}.md, достаёт из них цитаты,
ищет каждую в data/input/requirements.md через quote_check.locate (эксперимент 03).
В терминал печатает только счётчики. Тексты цитат не выводятся и не сохраняются.

Эвристика извлечения цитат (по порядку, найденное место маскируется и повторно не берётся):
  1. Всё после заголовка «Предлагаемые формулировки ...» отбрасывается: там модель
     предлагает собственные формулировки, а не цитирует документ.
  2. Фрагменты в «ёлочках» «...» (в пределах одной строки).
  3. Строки-цитаты Markdown (начинаются с «>»), в которых нет «ёлочек»: берётся вся строка
     без префикса-метки вида «Ф-2:» и без «>».
  4. Фрагменты в обратных кавычках `...`, если после снятия разметки в них не меньше 12 символов
     или есть пробел (короткие идентификаторы `amount`, `cardId` цитатами не считаются).
  5. Фрагменты в прямых или типографских двойных кавычках "..." / “...” длиной от 15 символов
     вне обратных кавычек (JSON-примеры внутри обратных кавычек так не попадают).
Подготовка каждой цитаты: снимается жирный и курсив (** и *), цитата режется на сегменты по
многоточию («…», «...»), по « / » и « | » (так модель склеивает ячейки таблиц и пропуски).
Сегменты короче 6 символов (после нормализации) отбрасываются. Если сегментов не осталось,
цитата не учитывается.
Вердикт по цитате: exact, если все сегменты найдены точно; fuzzy, если все найдены, но хотя бы
один нечётко (rapidfuzz, порог 90); not_found, если хотя бы один сегмент не найден.

Важно: «exact» считается после нормализации верификатора quote_check.normalize (убираются
markdown-символы * _ ` | # >, ёлочки и типографские кавычки приводятся к одному виду,
тире унифицируются, регистр игнорируется, схлопываются пробелы и пробелы внутри чисел).
Это не побайтное совпадение с исходной строкой.

Запуск (из корня репозитория):
    $env:PYTHONUTF8=1; uv run --group experiments --with pydantic python experiments/11-baseline-one-prompt/score_quotes.py
Также пишет счётчики в experiments/out/11/score_quotes.json.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'experiments' / '03-proto'))
from quote_check import locate, normalize  # noqa: E402

DOC = ROOT / 'data' / 'input' / 'requirements.md'
OUT = ROOT / 'experiments' / 'out' / '11'
MIN_SEG = 6

GUIL = re.compile(r'«([^«»\n]+)»')
BT = re.compile(r'`([^`\n]+)`')
DQ = re.compile(r'["“„]([^"“”„\n]{15,})["”“]')
LABEL = re.compile(r'^[^:«»]{1,25}:\s*')
SPLIT = re.compile(r'…|\.\.\.|\s/\s|\s\|\s')


def mask(s, m):
    return s[:m.start()] + ' ' * (m.end() - m.start()) + s[m.end():]


def extract(text):
    cut = re.search(r'^#+\s*Предлагаемые формулировки.*$', text, re.M)
    if cut:
        text = text[:cut.start()]
    text = text.replace('**', '')
    quotes = []  # (kind, text)
    work = text
    for m in list(GUIL.finditer(work)):
        quotes.append(('guillemet', m.group(1)))
    work = GUIL.sub(lambda m: ' ' * len(m.group(0)), work)
    lines = work.split('\n')
    orig = text.split('\n')
    for ln, o in zip(lines, orig):
        if ln.lstrip().startswith('>') and '«' not in o:
            body = ln.lstrip()[1:].strip()
            body = LABEL.sub('', body)
            if body:
                quotes.append(('blockquote', body))
    work = '\n'.join(' ' * len(l) if l.lstrip().startswith('>') else l for l in lines)
    for m in list(BT.finditer(work)):
        t = m.group(1)
        if len(t.strip()) >= 12 or ' ' in t.strip():
            quotes.append(('backtick', t))
    work = BT.sub(lambda m: ' ' * len(m.group(0)), work)
    for m in DQ.finditer(work):
        quotes.append(('dquote', m.group(1)))
    return quotes


def check(doc, quote):
    segs = [s for s in SPLIT.split(quote) if len(normalize(s)[0].strip()) >= MIN_SEG]
    if not segs:
        return None
    kinds = [locate(doc, s)[0] for s in segs]
    if 'not_found' in kinds:
        return 'not_found'
    if 'fuzzy' in kinds:
        return 'fuzzy'
    return 'exact'


def main():
    doc = DOC.read_text(encoding='utf-8')
    res = {}
    tot = {'total': 0, 'exact': 0, 'fuzzy': 0, 'not_found': 0}
    by_kind = {}
    for i in (1, 2, 3):
        text = (OUT / f'baseline_run{i}.md').read_text(encoding='utf-8')
        c = {'total': 0, 'exact': 0, 'fuzzy': 0, 'not_found': 0}
        for kind, q in extract(text):
            v = check(doc, q)
            if v is None:
                continue
            c['total'] += 1
            c[v] += 1
            bk = by_kind.setdefault(kind, {'total': 0, 'exact': 0, 'fuzzy': 0, 'not_found': 0})
            bk['total'] += 1
            bk[v] += 1
        res[f'run{i}'] = c
        for k in tot:
            tot[k] += c[k]
    res['overall'] = tot
    for name, c in res.items():
        t = c['total'] or 1
        c['share_exact'] = round(c['exact'] / t, 4)
        c['share_exact_or_fuzzy'] = round((c['exact'] + c['fuzzy']) / t, 4)
    res['by_extraction_kind_overall'] = by_kind
    (OUT / 'score_quotes.json').write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding='utf-8')
    for name in ('run1', 'run2', 'run3', 'overall'):
        c = res[name]
        print(f"{name}: total={c['total']} exact={c['exact']} fuzzy={c['fuzzy']} not_found={c['not_found']} "
              f"share_exact={c['share_exact']:.1%} share_exact_or_fuzzy={c['share_exact_or_fuzzy']:.1%}")
    for k, c in by_kind.items():
        print(f"  kind {k}: total={c['total']} exact={c['exact']} fuzzy={c['fuzzy']} not_found={c['not_found']}")


if __name__ == '__main__':
    main()
