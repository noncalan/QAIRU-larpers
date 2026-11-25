#let r = json(bytes(sys.inputs.report))
#set document(title: "Отчёт о проверке требований")
#set page(paper: "a4", margin: 2cm, numbering: "1 / 1")
#set text(lang: "ru", font: ("Libertinus Serif", "New Computer Modern"), size: 10pt)
= Отчёт о проверке требований
*Документ:* #r.doc · *Модель:* #raw(r.model) · *Версия агента:* #raw(r.version)
== Находки
#table(columns: (auto, auto, 1fr, auto), [*ID*], [*Место*], [*Цитата*], [*Важность*],
  ..r.findings.map(f => (f.id, f.loc, ["#f.quote"], f.sev)).flatten())
