"""Прототип верификатора цитат: нормализация с картой смещений + точный поиск + rapidfuzz."""
import re, sys, unicodedata
from rapidfuzz import fuzz

TRANS = str.maketrans({'ё': 'е', 'Ё': 'Е', '«': '"', '»': '"', '“': '"', '”': '"', '„': '"',
                       '‘': "'", '’': "'", '—': '-', '–': '-', '‑': '-', '−': '-',
                       ' ': ' ', ' ': ' ', ' ': ' '})
DROP = set('*_`|#>')  # markdown-разметка и разделители таблиц

def normalize(text):
    """Возвращает (норм. строка, список: индекс норм. символа -> индекс в оригинале)."""
    out, idx = [], []
    prev_space = True
    for i, ch in enumerate(text):
        ch = unicodedata.normalize('NFKC', ch).translate(TRANS).lower()
        if ch in DROP:
            ch = ' '
        if ch.isspace():
            if prev_space:
                continue
            ch = ' '
            prev_space = True
        else:
            prev_space = False
        out.append(ch); idx.append(i)
    s = ''.join(out)
    # "1 000 000" -> "1000000": убираем пробел внутри числа
    keep = [k for k in range(len(s)) if not (s[k] == ' ' and 0 < k < len(s) - 1
            and s[k-1].isdigit() and s[k+1].isdigit())]
    return ''.join(s[k] for k in keep), [idx[k] for k in keep]

def locate(doc, quote, threshold=90.0):
    nd, m = normalize(doc)
    nq, _ = normalize(quote)
    nq = nq.strip()
    pos = nd.find(nq)
    if pos >= 0:
        return 'exact', 100.0, m[pos], m[pos + len(nq) - 1] + 1
    al = fuzz.partial_ratio_alignment(nq, nd, score_cutoff=threshold)
    if al is None:
        best = fuzz.partial_ratio_alignment(nq, nd)
        return 'not_found', best.score, None, None
    return 'fuzzy', al.score, m[al.dest_start], m[al.dest_end - 1] + 1

def line_of(doc, off):
    return doc.count('\n', 0, off) + 1

if __name__ == '__main__':
    doc = open(sys.argv[1], encoding='utf-8').read()
    tests = [
        'Месяц определяется по дате совершения покупки в Алматы.',            # точная
        'по умолчанию уведомление выключено',                                 # регистр
        'Держатель активной карты KZT задаёт целый порог от 1000 до 1000000 ₸ включительно',  # цифры без пробелов
        'Карточный процессинг -> учёт расходов -> сервис push',               # стрелка/ASCII
        'Сервис расходов → push: передать последние 4 цифры карты и сумму',   # мелкая правка LLM
        'На карту приходится не более одного push в календарный месяц',       # перефраз
        'Push отправляется мгновенно в момент покупки',                       # выдумка
        '| Расходы 48 500 ₸, порог 50 000 ₸ | Провести покупку на 1 500 ₸ |',   # строка таблицы
    ]
    for q in tests:
        kind, score, s, e = locate(doc, q)
        where = f'строки {line_of(doc, s)}-{line_of(doc, e)}, символы {s}-{e}: «{doc[s:e][:70]}»' if s is not None else ''
        print(f'{kind:9} {score:5.1f}  {q[:60]!r}\n          {where}')
