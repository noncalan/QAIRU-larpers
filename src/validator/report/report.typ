// Вёрстка PDF-отчёта. Все тексты и числа готовит render_pdf.py (модель вида приходит в sys.inputs.report);
// здесь только раскладка на страницы. Шрифты: Liberation (OFL), их путь передаёт render_pdf.py.

#let d = json(bytes(sys.inputs.report))

#let ink = rgb("#1f2933")
#let muted = rgb("#5b6773")
#let rule = rgb("#cfd6dd")
#let head-fill = rgb("#eef1f4")
#let tones = (
  critical: rgb("#b3261e"),
  major: rgb("#a35f00"),
  minor: rgb("#4f6b7a"),
  P0: rgb("#b3261e"),
  P1: rgb("#a35f00"),
  P2: rgb("#4f6b7a"),
  green: rgb("#2e7d32"),
  yellow: rgb("#c98a00"),
  red: rgb("#c62828"),
)

#set document(title: d.title + ": " + d.document, author: "QAIRU validator")
#set page(
  paper: "a4",
  margin: (x: 17mm, top: 22mm, bottom: 20mm),
  header: {
    set text(size: 7.5pt, fill: muted)
    grid(
      columns: (1fr, auto),
      [#d.title · #d.document],
      [#d.date · агент #d.version],
    )
    v(-4pt)
    line(length: 100%, stroke: 0.5pt + rule)
  },
  footer: {
    set text(size: 7.5pt, fill: muted)
    line(length: 100%, stroke: 0.5pt + rule)
    v(-4pt)
    grid(
      columns: (1fr, auto),
      [QAIRU validator #d.version],
      context [стр. #counter(page).display() из #counter(page).final().first()],
    )
  },
)
#set text(font: "Liberation Sans", lang: "ru", size: 9pt, fill: ink)
#set par(leading: 0.6em, spacing: 0.9em)
#set list(indent: 4pt, body-indent: 6pt, spacing: 0.6em)

#show heading.where(level: 1): it => block(above: 16pt, below: 8pt, sticky: true, width: 100%)[
  #text(size: 13pt, weight: "bold")[#it.body]
  #v(-3pt)
  #line(length: 100%, stroke: 0.8pt + ink)
]
#show heading.where(level: 2): it => block(above: 12pt, below: 6pt, sticky: true)[
  #text(size: 10.5pt, weight: "bold")[#it.body]
]

#let mono(c) = text(font: "Liberation Mono", size: 7.5pt)[#c]

// ---------------------------------------------------------------- таблицы

#let data-table(b) = {
  set text(size: 8pt)
  let head = b.head.map(h => table.cell(fill: head-fill)[#strong(h)])
  // Переносы слов только в узких колонках: в цитатах слова остаются целыми, их копируют из PDF.
  let fmt(i, c) = if i in b.mono { mono(c) } else if i in b.hyph { text(hyphenate: true)[#c] } else [#c]
  let cells = b.rows.map(r => r.enumerate().map(((i, c)) => fmt(i, c))).flatten()
  table(
    columns: b.widths.map(w => w * 1fr),
    inset: 4pt,
    stroke: 0.4pt + rule,
    table.header(..head),
    ..cells,
  )
}

#let kv-table(b) = {
  set text(size: 8.5pt)
  table(
    columns: (30%, 1fr),
    inset: 4pt,
    stroke: 0.4pt + rule,
    ..b.rows.map(r => (table.cell(fill: head-fill)[#strong(r.at(0))], [#r.at(1)])).flatten(),
  )
}

// ---------------------------------------------------------------- карточки находок и вопросов

#let value(v) = {
  if type(v) == str {
    v
  } else if "table" in v {
    data-table(v.table)
  } else {
    text(style: "italic")[«#v.quote»]
    linebreak()
    text(size: 7.5pt, fill: if v.ok { muted } else { tones.critical })[#v.meta]
  }
}

#let card(b) = {
  let tone = tones.at(b.tone)
  block(width: 100%, stroke: 0.6pt + rule, radius: 3pt, above: 9pt, below: 6pt)[
    #block(
      width: 100%,
      fill: tone.lighten(88%),
      inset: (x: 8pt, y: 5pt),
      radius: (top: 3pt),
      stroke: (bottom: 0.6pt + rule),
      above: 0pt,
      below: 0pt,
      sticky: true,
    )[
      #box(width: 6pt, height: 6pt, fill: tone, radius: 3pt)
      #h(3pt)
      #strong(b.head)
    ]
    #block(inset: 8pt, width: 100%, above: 0pt, below: 0pt)[
      #set text(size: 8.5pt)
      #grid(
        columns: (26%, 1fr),
        column-gutter: 8pt,
        row-gutter: 5pt,
        ..b.rows.map(r => (text(fill: muted)[#r.at(0)], value(r.at(1)))).flatten(),
      )
    ]
  ]
}

// ---------------------------------------------------------------- светофор

#let light-badge(key, headline, size: 14pt) = {
  let tone = tones.at(key)
  block(width: 100%, fill: tone.lighten(86%), stroke: 1pt + tone, radius: 6pt, inset: 12pt, breakable: false)[
    #grid(
      columns: (auto, 1fr),
      column-gutter: 12pt,
      align: horizon,
      circle(radius: 11pt, fill: tone),
      text(size: size, weight: "bold")[#headline],
    )
  ]
}

#let render-block(b) = {
  if b.t == "para" {
    par[#b.text]
  } else if b.t == "h2" {
    heading(level: 2)[#b.text]
  } else if b.t == "note" {
    par(text(size: 8pt, fill: muted)[#b.text])
  } else if b.t == "bullets" {
    list(..b.items.map(i => [#i]))
  } else if b.t == "table" {
    data-table(b)
  } else if b.t == "kv" {
    kv-table(b)
  } else if b.t == "card" {
    card(b)
  } else if b.t == "verdict" {
    light-badge(b.light, b.text, size: 11pt)
  }
}

// ---------------------------------------------------------------- первая страница

#let first-page(f) = {
  light-badge(f.light, f.headline)
  if f.stats.len() > 0 {
    v(8pt)
    grid(
      columns: f.stats.map(_ => 1fr),
      column-gutter: 6pt,
      ..f.stats.map(s => block(width: 100%, stroke: 0.5pt + rule, radius: 4pt, inset: 7pt, breakable: false)[
        #text(size: 20pt, weight: "bold")[#s.at(1)] \
        #text(size: 7.5pt, fill: muted)[#s.at(0)]
      ]),
    )
    if f.at("clarify", default: "") != "" {
      v(6pt)
      par[#text(size: 9pt)[#strong[Уточнения автора.] #f.clarify]]
    }
    heading(level: 2)[Что исправить первым]
    if f.fix.len() == 0 {
      par[#f.fix_empty]
    }
    for (i, x) in f.fix.enumerate() {
      block(
        stroke: (left: 2.5pt + tones.at(x.tone)),
        inset: (left: 8pt, y: 3pt),
        above: 6pt,
        below: 6pt,
        width: 100%,
        breakable: false,
      )[
        #set text(size: 8.5pt)
        #strong[#(i + 1). #x.id] · #x.head: #x.text
        #if x.quote != "" [
          \ #text(fill: muted)[Цитата:] #text(style: "italic")[«#x.quote»] #text(fill: muted)[(#x.place)]
        ]
        #if x.suggestion != "" [
          \ #text(fill: muted)[Предложенная формулировка:] #x.suggestion
        ]
        #if x.questions != "" [
          \ #text(fill: muted)[Вопрос автору:] #x.questions
        ]
      ]
    }
    heading(level: 2)[Какие вопросы задать автору первыми]
    if f.ask.len() == 0 {
      par[#f.ask_empty]
    }
    for (i, x) in f.ask.enumerate() {
      block(
        stroke: (left: 2.5pt + tones.at(x.tone)),
        inset: (left: 8pt, y: 3pt),
        above: 6pt,
        below: 6pt,
        width: 100%,
        breakable: false,
      )[
        #set text(size: 8.5pt)
        #strong[#(i + 1). #x.id] · #x.head: #x.text
        #if x.options.len() > 0 [
          #for o in x.options [
            \ #h(8pt) #text(fill: muted)[–] #o
          ]
        ]
      ]
    }
  }
}

// ---------------------------------------------------------------- документ

#text(size: 19pt, weight: "bold")[#d.title]
#v(-2pt)
#text(size: 12pt)[#d.document]
#v(2pt)
#text(size: 8pt, fill: muted)[#d.intro]

#heading(level: 1)[#d.first_title]
#first-page(d.first)

#for (i, s) in d.sections.enumerate() {
  if i == 0 { pagebreak() }
  heading(level: 1)[#s.title]
  for b in s.blocks { render-block(b) }
}
