from z3 import *
from datetime import datetime
from zoneinfo import ZoneInfo

print('=== #1 Ф-2 vs AC-2: оракул по сценарию ===')
ALA = ZoneInfo('Asia/Almaty')
purchase = datetime(2027, 1, 31, 23, 58, tzinfo=ALA)   # «Покупка 31 января в 23:58»
posted   = datetime(2027, 2, 1, 0, 3, tzinfo=ALA)      # «Провести 1 февраля в 00:03 по Алматы»
bucket = Int('bucket_month')
s = Solver(); s.set(unsat_core=True)
s.assert_and_track(bucket == purchase.month, 'F2: месяц = месяц даты совершения покупки')
s.assert_and_track(bucket == 2,               'AC2: ожидается рост февральского счётчика')
print(s.check(), s.unsat_core())

print('\n=== #2 Ф-3 vs Ф-4: деонтический конфликт, Z3 сам ищет свидетеля ===')
N = 4                                            # ограниченная трасса из 4 событий в одном месяце
spent = [Int(f'spent_{i}') for i in range(N+1)]  # накопленные расходы после события i
thr   = [Int(f'thr_{i}')   for i in range(N+1)]  # порог после события i
push  = [Bool(f'push_{i}') for i in range(N+1)]
kind  = [Int(f'kind_{i}')  for i in range(N+1)]  # 0 = покупка, 1 = смена порога
s = Solver(); s.set(unsat_core=True)
dom = [spent[0] == 0, Not(push[0]), And(thr[0] >= 1000, thr[0] <= 1000000), kind[0] == 0]
for i in range(1, N+1):
    dom += [Or(kind[i] == 0, kind[i] == 1),
            Implies(kind[i] == 0, And(spent[i] > spent[i-1], thr[i] == thr[i-1])),
            Implies(kind[i] == 1, And(spent[i] == spent[i-1], thr[i] >= 1000, thr[i] <= 1000000,
                                      thr[i] != thr[i-1], Not(push[i])))]
s.assert_and_track(And(dom), 'DOM: допустимые входы (Ф-1, Ф-2, особое правило 3)')
cross = lambda i: And(kind[i] == 0, spent[i-1] < thr[i], spent[i] >= thr[i])
# Ф-3 (1): переход снизу вверх -> push (обязанность)  — оставлено для полноты, конфликт даёт пара ниже
# Ф-3 (2): не больше одного push за календарный месяц (запрет)
s.assert_and_track(Sum([If(p, 1, 0) for p in push]) <= 1, 'F3b: максимум один push за месяц')
# Ф-4: после смены порога, если накопленное < нового порога, следующее достижение -> push
#       «независимо от истории предыдущей настройки» (обязанность)
f4 = []
for j in range(1, N+1):
    for k in range(j+1, N+1):
        first_after = And([Not(cross(m)) for m in range(j+1, k)])
        f4.append(Implies(And(kind[j] == 1, spent[j] < thr[j], cross(k), first_after), push[k]))
s.assert_and_track(And(f4), 'F4: следующее достижение после смены порога -> push')
# Ф-3 (1): переход снизу вверх -> push
# Ф-3 (1) в согласованном с (2) прочтении: переход -> push, если в этом месяце push ещё не было
no_prior = lambda i: And([Not(push[m]) for m in range(1, i)])
F3a = And([Implies(And(cross(i), no_prior(i)), push[i]) for i in range(1, N+1)])
s.assert_and_track(F3a, 'F3a: переход через порог -> push (если push в месяце ещё не было)')
# Ф-3/BPMN: push бывает только при достижении порога («при достижении отправка push»)
s.assert_and_track(And([Implies(push[i], cross(i)) for i in range(1, N+1)]), 'F3c/BPMN: push только при достижении')
# Форма ситуации-свидетеля: push -> смена порога (накопленное < нового) -> новое достижение
shape = Or([And(push[i], kind[j] == 1, spent[j] < thr[j], cross(k))
            for i in range(1, N+1) for j in range(i+1, N+1) for k in range(j+1, N+1)])
# 1) Невакуумность: ситуация допустима, если убрать пару F3b/F4
v = Solver(); v.add(And(dom), shape, F3a)
print('ситуация допустима (без F3b и F4):', v.check())
# 2) Со всеми требованиями — противоречие
s.assert_and_track(shape, 'SCN: push, затем смена порога выше накопленного, затем новое достижение')
r = s.check(); print('все требования на этой ситуации:', r)
if r == unsat: print('unsat core:', s.unsat_core())
# 3) Человекочитаемый свидетель (его предлагает LLM, Z3 только проверяет)
h = Solver(); h.add(And(dom), And(f4), F3a)
h.add(kind[1] == 0, spent[1] == 25000, thr[0] == 20000,      # 10.01: покупки на 25 000 при пороге 20 000
      kind[2] == 1, thr[2] == 50000,                            # 15.01: порог поднят до 50 000
      kind[3] == 0, spent[3] == 55000,                          # 20.01: ещё 30 000, итог 55 000
      kind[4] == 0, spent[4] == 56000)
print('свидетель 25 000 / 50 000 / 55 000 без F3b:', h.check(),
      '; число push =', h.model().eval(Sum([If(p, 1, 0) for p in push]), model_completion=True) if h.check() == sat else '')
h.add(Sum([If(p, 1, 0) for p in push]) <= 1)
print('тот же свидетель с F3b:', h.check())
print('\n=== #34 «проведённые»: две трактовки и достижимость цели ===')
lag = Int('posting_lag_min')          # от совершения покупки до «проведения», минут
react = Int('push_delay_min')         # от события процессинга до передачи push (НФТ)
goal_eps = 1                          # «в момент покупки» ~ не позже 1 мин (допущение, вынести в отчёт)
for name, lag_c in [('I1: проведена = авторизована', lag == 0),
                    ('I2: проведена = клиринг/posting (1–3 дня, доменный чек-лист)', And(lag >= 1440, lag <= 4320))]:
    s = Solver(); s.set(unsat_core=True)
    s.assert_and_track(lag_c, 'TERM: ' + name)
    s.assert_and_track(And(react >= 0, react <= 1), 'NFR: передача push в течение минуты после события')
    s.assert_and_track(lag + react <= goal_eps, 'GOAL: узнать в момент покупки')
    r = s.check(); print(name, '->', r, '' if r == sat else s.unsat_core())
# Свидетель из самого документа: AC-2 фиксирует проведение через 5 минут после совершения
s = Solver(); s.set(unsat_core=True)
s.assert_and_track(lag == 5, 'AC2: совершена 23:58, проведена 00:03')
s.assert_and_track(And(react >= 0, react <= 1), 'NFR')
s.assert_and_track(lag + react <= goal_eps, 'GOAL')
print('AC2-свидетель ->', s.check(), s.unsat_core())
