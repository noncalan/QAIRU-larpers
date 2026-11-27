import { useEffect, useRef, useState } from "react";
import {
  REPO, gh, tree, media, SECTIONS, HERO_CMD, HERO_TERMINAL, PROBLEM, AGENT, STEPS, WHO,
  NUMBERS, COMPARE, LIMITS, FIRST_PAGE, REPORT_LINKS, DEMO_CHAPTERS, PATHS, ALSO, OUT_TREE,
} from "./content.js";
import Intro from "./Intro.jsx";
import alikhanImg from "./assets/team/alikhan.jpg";
import ramazanImg from "./assets/team/ramazan.jpg";
import nurdauletImg from "./assets/team/nurdaulet.jpg";
import { LOGO_N, logoSvgRects } from "./logo.js";

const TEAM = [
  ["Айдынулы Алихан", alikhanImg, "50% 35%"],
  ["Габдулгумаров Рамазан", ramazanImg, "50% 40%"],
  ["Бейсенбек Нурдаулет", nurdauletImg, "50% 50%"],
];

const LOGO_RECTS = logoSvgRects();
function Logo({ size = 24 }) {
  return (
    <svg className="logo" width={size} height={size} viewBox={`-0.5 -0.5 ${LOGO_N + 1} ${LOGO_N + 1}`} aria-hidden="true" shapeRendering="crispEdges">
      {LOGO_RECTS.map((r, i) => <rect key={i} x={r.x} y={r.y} width={r.s} height={r.s} fill={r.fill} />)}
    </svg>
  );
}

const reduced = () =>
  typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/* ---------- утилиты ---------- */

function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand("copy"); } finally { ta.remove(); }
  return Promise.resolve();
}

function CopyButton({ text, label = "Копировать", small }) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      className={`copy${small ? " copy-sm" : ""}${done ? " is-done" : ""}`}
      onClick={() => copyText(text).then(() => { setDone(true); setTimeout(() => setDone(false), 1500); })}
      aria-label={small ? `${label}: ${text}` : undefined}
    >
      <svg viewBox="0 0 16 16" aria-hidden="true">
        {done ? (
          <path d="M3 8.5l3 3 7-7" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
        ) : (
          <>
            <rect x="5" y="5" width="8.5" height="8.5" rx="1.6" fill="none" stroke="currentColor" strokeWidth="1.4" />
            <path d="M10.5 3.2V3a1.5 1.5 0 0 0-1.5-1.5H3A1.5 1.5 0 0 0 1.5 3v6A1.5 1.5 0 0 0 3 10.5h.2" fill="none" stroke="currentColor" strokeWidth="1.4" />
          </>
        )}
      </svg>
      {!small && <span>{done ? "Скопировано" : label}</span>}
    </button>
  );
}

function useInView(options = { threshold: 0.3 }) {
  const ref = useRef(null);
  const [seen, setSeen] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const io = new IntersectionObserver(([e]) => { if (e.isIntersecting) { setSeen(true); io.disconnect(); } }, options);
    io.observe(el);
    return () => io.disconnect();
  }, []);
  return [ref, seen];
}

function Counter({ n, d = 0, suf = "", pre = "" }) {
  const [ref, seen] = useInView({ threshold: 0.6 });
  const [v, setV] = useState(0);
  useEffect(() => {
    if (!seen) return;
    if (reduced()) { setV(n); return; }
    let raf;
    const t0 = performance.now();
    const dur = 1300;
    const tick = (t) => {
      const k = Math.min(1, (t - t0) / dur);
      setV(n * (1 - Math.pow(1 - k, 3)));
      if (k < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [seen, n]);
  return (
    <strong ref={ref} className="counter">
      {pre}{v.toFixed(d).replace(".", ",")}{suf}
    </strong>
  );
}

function SectionHead({ idx, eyebrow, title, children }) {
  return (
    <div className="sec-head" data-reveal>
      <span className="eyebrow"><span className="idx">{String(idx).padStart(2, "0")}</span>{eyebrow}</span>
      <h2>{title}</h2>
      {children && <p>{children}</p>}
    </div>
  );
}

/* ---------- шапка и строка состояния ---------- */

function TopNav() {
  return (
    <header className="topnav">
      <a className="brand" href="#top" aria-label="QAIRU validator, в начало">
        <Logo size={26} />
        QAIRU
      </a>
      <nav aria-label="Разделы">
        <a href="#pipeline">Как устроен</a>
        <a href="#quality">Качество</a>
        <a href="#report">Отчёт</a>
        <a href={REPO} className="hide-sm">GitHub</a>
      </nav>
      <a className="nav-cta" href="#install">Запустить</a>
    </header>
  );
}

function StatusBar({ active }) {
  const navRef = useRef(null);
  useEffect(() => {
    const nav = navRef.current;
    const el = nav?.querySelector("a.on");
    if (nav && el && nav.scrollWidth > nav.clientWidth) {
      nav.scrollTo({ left: el.offsetLeft - nav.offsetLeft - 24, behavior: reduced() ? "auto" : "smooth" });
    }
  }, [active]);
  return (
    <div className="statusbar" aria-label="Навигация по разделам">
      <span className="sb-name"><i className="dot" aria-hidden="true" />qairu validator <span className="muted">v1.0</span></span>
      <nav className="sb-index" ref={navRef}>
        {SECTIONS.map(([id, label], i) => (
          <a key={id} href={`#${id}`} className={active === id ? "on" : ""}>
            <span className="muted">{i + 1}</span> {label}
          </a>
        ))}
      </nav>
      <span className="sb-right">
        <a href={gh("LICENSE")}>MIT</a>
        <a href={REPO}>github</a>
      </span>
    </div>
  );
}

/* ---------- hero ---------- */

function HeroTerminal() {
  const [shown, setShown] = useState(reduced() ? HERO_TERMINAL.length : 0);
  const [typed, setTyped] = useState(reduced() ? 999 : 0);
  const [cycle, setCycle] = useState(0);

  useEffect(() => {
    if (reduced()) return;
    let timers = [];
    const cmd = HERO_TERMINAL[1].s;
    setShown(1); setTyped(0);
    let t = 700;
    for (let i = 1; i <= cmd.length; i += 2) {
      timers.push(setTimeout(() => setTyped(i), t));
      t += 22;
    }
    timers.push(setTimeout(() => { setTyped(cmd.length); setShown(2); }, t + 200));
    t += 1400; // «работа» агента
    for (let k = 3; k <= HERO_TERMINAL.length; k++) {
      timers.push(setTimeout(() => setShown(k), t));
      t += 380;
    }
    timers.push(setTimeout(() => setCycle((c) => c + 1), t + 9000));
    return () => timers.forEach(clearTimeout);
  }, [cycle]);

  const running = shown === 2;
  return (
    <div className="term hero-term">
      <div className="term-bar">
        <span className="lights" aria-hidden="true"><i /><i /><i /></span>
        <span>terminal · replay живого прогона</span>
        <CopyButton text={HERO_CMD} small label="Копировать команду" />
      </div>
      <pre className="term-body" aria-label="Запись запуска агента">
        {HERO_TERMINAL.slice(0, shown).map((l, i) => {
          if (l.t === "cmd") {
            const s = i === 1 && shown === 2 ? l.s : l.s.slice(0, typed);
            return <div key={i} className="ln cmd"><span className="ps">$</span>{s}{shown <= 2 && <span className="caret" />}</div>;
          }
          return <div key={i} className={`ln ${l.t}`}>{l.s}</div>;
        })}
        {running && <div className="ln run"><span className="spin" aria-hidden="true" /> проверка: 10 шагов конвейера…</div>}
      </pre>
    </div>
  );
}

function HeroSheet() {
  return (
    <figure className="sheet" aria-label="Фрагмент первой страницы отчёта по requirements.pdf">
      <div className="sheet-top"><span>reports/v1.0/report.pdf · итог</span><span>v1.0</span></div>
      <div className="verdict">
        <div className="light" aria-hidden="true"><i className="on" /><i /><i /></div>
        <div><b>Начинать разработку и тестирование нельзя</b><span>6 критичных нарушений мешают написать тест-кейсы.</span></div>
      </div>
      <div className="counts">
        <div><strong>58</strong><small>требований</small></div>
        <div><strong>46</strong><small>нарушений</small></div>
        <div><strong className="crit">6</strong><small>критичных</small></div>
        <div><strong>14</strong><small>вопросов</small></div>
      </div>
      <div className="finding">
        <div className="tags"><span className="tag">F-02</span><span className="tag crit">критично</span><span className="tag">противоречие</span></div>
        <b>Месяц покупки по Ф-2 и по критерию приёмки различается</b>
        <blockquote>
          <span className="hl">«Месяц определяется по дате совершения покупки в Алматы.»</span>
          <cite>«Требования к изменениям › Функциональные требования», стр. 2 · R-06.1, R-14</cite>
        </blockquote>
        <span className="stamp"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 8.5l3 3 6-7" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" /></svg>цитата сверена кодом</span>
      </div>
    </figure>
  );
}

function Hero() {
  return (
    <section className="hero" id="top">
      <div className="grid-floor" aria-hidden="true"><div className="plane" /></div>
      <div className="glow" aria-hidden="true" />
      <div className="wrap hero-inner">
        <span className="pill" data-reveal>
          <span className="pill-k">BCC-HUB x MNU AI Agent Challenge 2026</span>
          <span>кейс AI Requirements Validator</span>
        </span>
        <h1 data-reveal>Ревью ТЗ <em>с доказательствами</em></h1>
        <p className="lede" data-reveal>
          CLI-агент читает документ требований в PDF, DOCX или Markdown, режет его на отдельные требования,
          проверяет каждое и весь набор по ISO/IEC/IEEE 29148 и IEEE 830 и пишет отчёт на русском:
          что нарушено, где именно в документе, что исправить первым и какие вопросы задать автору.
        </p>
        <div className="hero-actions" data-reveal>
          <a className="btn btn-primary" href="#install">Запустить за 5 минут <span aria-hidden="true">→</span></a>
          <div className="cmdline">
            <span className="ps">$</span><code>{HERO_CMD}</code>
            <CopyButton text={HERO_CMD} small label="Копировать команду" />
          </div>
        </div>
        <p className="meta" data-reveal>
          <a href={media("report.pdf")}>Отчёт на сдачу (PDF)</a> · Python 3.11+ через uv · без ключа, с подпиской Claude или с ключом API · MIT
        </p>

        <div className="chain" data-reveal aria-label="Принцип работы">
          <span><b>модель</b> предлагает</span><i aria-hidden="true" /><span><b>код</b> проверяет</span><i aria-hidden="true" /><span><b>человек</b> решает</span>
        </div>

        <div className="hero-fig" data-reveal>
          <HeroTerminal />
          <HeroSheet />
        </div>
        <p className="fig-cap">Рис. 1. Запуск на requirements.pdf из записи терминала и фрагмент первой страницы отчёта на сдачу v1.0. Вывод настоящий.</p>
      </div>
    </section>
  );
}

/* ---------- секции ---------- */

function Problem() {
  return (
    <section id="problem">
      <div className="wrap">
        <SectionHead idx={1} eyebrow="Проблема для банка" title="Дефекты требований находят поздно, когда исправлять дорого">
          Ревью ТЗ в банке медленное и зависит от проверяющего. Пропущенная неясность всплывает на тестировании, когда код уже написан.
        </SectionHead>
        <div className="facts">
          {PROBLEM.map((f) => (
            <div className="fact" key={f.v} data-reveal>
              <strong>{f.v}</strong>
              <p>{f.p}</p>
            </div>
          ))}
        </div>
        <p className="src" data-reveal>
          Источники: Boehm и Basili, 2001; Langenfeld и др., 2016, табл. 3; Doolan, 1992. Ставка 2 521 ₸/ч по hh.kz.
          Подробно в <a href={gh("docs/value.md")}>docs/value.md</a>. Время ручного ревью это оценка по опубликованным скоростям, на людях мы его не замеряли.
        </p>
      </div>
    </section>
  );
}

function Agent() {
  return (
    <section id="agent">
      <div className="wrap">
        <SectionHead idx={2} eyebrow="Что делает агент" title="На входе ТЗ, на выходе отчёт с находками, цитатами и вопросами">
          Агент выдаёт не мнения, а находки, которые можно проверить: цитата, пункт стандарта, пример-свидетель и следующий шаг.
        </SectionHead>
        <div className="cards">
          {AGENT.map(([k, h, p], i) => (
            <article className="card" key={k} data-reveal style={{ "--d": `${(i % 3) * 70}ms` }}>
              <span className="k">{String(i + 1).padStart(2, "0")} · {k}</span>
              <h3>{h}</h3>
              <p>{p}</p>
            </article>
          ))}
        </div>
      </div>
    </section>
  );
}

function Pipeline() {
  const [active, setActive] = useState(0);
  const refs = useRef([]);
  useEffect(() => {
    const io = new IntersectionObserver(
      (entries) => entries.forEach((e) => { if (e.isIntersecting) setActive(Number(e.target.dataset.i)); }),
      { rootMargin: "-45% 0px -45% 0px" },
    );
    refs.current.forEach((el) => el && io.observe(el));
    return () => io.disconnect();
  }, []);
  const [, , who] = STEPS[active];
  return (
    <section id="pipeline">
      <div className="wrap split">
        <div className="sticky">
          <SectionHead idx={3} eyebrow="Конвейер" title="Модель предлагает, код проверяет">
            Модель делает то, что умеет только она: решает, где в документе требования, ищет дефекты, строит свидетеля и пишет вопросы.
            Всё, от чего зависит доверие к отчёту, делает код без модели.
          </SectionHead>
          <div className="rail" aria-hidden="true">
            {STEPS.map(([, , w], i) => (
              <span key={i} className={`node ${w}${i <= active ? " lit" : ""}${i === active ? " cur" : ""}`}>{i}</span>
            ))}
          </div>
          <div className="now" aria-live="polite">
            <span className={`who ${who}`}>{WHO[who]}</span>
            <span>шаг {active} из 9 · {STEPS[active][0]}</span>
          </div>
          <div className="legend">
            <span><span className="who code">код</span> без модели</span>
            <span><span className="who model">модель</span> предлагает</span>
            <span><span className="who both">модель + код</span> код принимает</span>
          </div>
          <p className="src">
            Подробно: <a href={gh("docs/architecture.md")}>docs/architecture.md</a>, решения с альтернативами: <a href={tree("docs/adr")}>docs/adr/</a>.
          </p>
        </div>
        <div>
          <ol className="steps">
            {STEPS.map(([h, d, w], i) => (
              <li key={h} ref={(el) => (refs.current[i] = el)} data-i={i} className={i === active ? "on" : i < active ? "past" : ""}>
                <span className="n">{i}</span>
                <span className="body"><b>{h}</b><span className="d">{d}</span></span>
                <span className={`who ${w}`}>{WHO[w]}</span>
              </li>
            ))}
          </ol>
          <details className="more">
            <summary>Полная схема конвейера <span className="muted">docs/img/pipeline.svg</span></summary>
            <a className="diagram" href={media("pipeline.svg")} target="_blank" rel="noreferrer">
              <img src={media("pipeline.svg")} alt="Схема конвейера агента: от защищённого входа до отчёта, с разделением шагов модели и кода" loading="lazy" />
            </a>
          </details>
        </div>
      </div>
    </section>
  );
}

function Quality() {
  const [tblRef, tblSeen] = useInView({ threshold: 0.2 });
  return (
    <section id="quality">
      <div className="wrap">
        <SectionHead idx={4} eyebrow="Качество" title="Цифры из прогонов, сильные и слабые стороны">
          Сравнили агента с одним запросом в чат на документе кейса по одному протоколу: по три прогона на финальном коде, эталон из 45 дефектов.
        </SectionHead>
        <div className="nums">
          {NUMBERS.map((x, i) => (
            <div className="num" key={i} data-reveal style={{ "--d": `${i * 80}ms` }}>
              <Counter {...x} />
              <p>{x.p}</p>
            </div>
          ))}
        </div>

        <div className={`compare${tblSeen ? " in" : ""}`} ref={tblRef} data-reveal>
          <div className="cmp-head">
            <span>Метрика</span><span>Один промпт</span><span>Агент</span><span>Кто лучше</span>
          </div>
          {COMPARE.map(([m, a, b, win, bars]) => (
            <div className="cmp-row" key={m}>
              <span className="m">{m}</span>
              <span className="v">
                {a}
                {bars && <span className="bar"><i style={{ "--w": bars[0] }} /></span>}
              </span>
              <span className="v agent">
                {b}
                {bars && <span className="bar"><i className="acc" style={{ "--w": bars[1] }} /></span>}
              </span>
              <span className={`win ${win}`}>{win === "agent" ? "агент" : win === "prompt" ? "один промпт" : "наравне"}</span>
            </div>
          ))}
        </div>
        <p className="src" data-reveal>
          Средние трёх прогонов стабильности по requirements.md. Отчёт на сдачу это отдельный прогон по PDF: 12,5 минуты, $2,75.
          Таблица и оговорки: <a href={gh("evals/results/summary.md")}>evals/results/summary.md</a>.
        </p>

        <div className="limits" data-reveal>
          <div className="limits-head">
            <span className="eyebrow">Честные ограничения</span>
            <h3>Чего агент не умеет сегодня</h3>
          </div>
          <ul>
            {LIMITS.map(([b, t]) => (
              <li key={b}><b>{b}</b> {t}</li>
            ))}
          </ul>
          <a className="src-link" href={`${REPO}#ограничения`}>Полный список в README, «Ограничения» →</a>
        </div>
      </div>
    </section>
  );
}

function Report() {
  return (
    <section id="report">
      <div className="wrap">
        <SectionHead idx={5} eyebrow="Пример отчёта" title="Отчёт, который собрал этот агент">
          Отчёт на сдачу v1.0 по requirements.pdf: 58 требований, 46 нарушений, 6 критичных, 14 вопросов автору. Каждая цитата сверена кодом с документом.
        </SectionHead>
        <div className="report-grid">
          <div className="term" data-reveal>
            <div className="term-bar">
              <span className="lights" aria-hidden="true"><i /><i /><i /></span>
              <span>reports/v1.0/report.md · первая страница</span>
            </div>
            <pre className="term-body md">
              {FIRST_PAGE.map((l, i) => (
                <div key={i} className={`ln ${l.startsWith("#") ? "h" : l.startsWith("🔴") ? "red" : /^\d\./.test(l) ? "item" : ""}`}>{l || " "}</div>
              ))}
            </pre>
          </div>
          <div className="report-side">
            <article className="evidence" data-reveal>
              <div className="tags"><span className="tag">F-02</span><span className="tag crit">критично</span><span className="tag">противоречие</span></div>
              <h3>Месяц покупки по Ф-2 и по критерию приёмки различается</h3>
              <blockquote>
                <span className="hl">«Месяц определяется по дате совершения покупки в Алматы.»</span>
                <cite>раздел «Требования к изменениям › Функциональные требования», стр. 2 · R-06.1, R-14</cite>
              </blockquote>
              <div className="ask">
                <span className="k">Q-01 · вопрос автору</span>
                <p>Покупка на 4 000 ₸ совершена 31 января в 23:58 и проведена 1 февраля в 00:03 по Алматы. В расходы какого месяца она входит?</p>
                <div className="opts"><span>A. В январь, по дате совершения</span><span>B. В февраль, по дате проведения</span></div>
              </div>
            </article>
            <div className="links-list" data-reveal>
              {REPORT_LINKS.map(([t, f, href]) => (
                <a key={f} href={href}><span>{t}</span><code>{f}</code></a>
              ))}
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

function Demo() {
  return (
    <section id="demo">
      <div className="wrap">
        <SectionHead idx={6} eyebrow="Демо" title="93 секунды работы агента в терминале">
          Одна команда на PDF, первая страница отчёта, битый файл, вопросы автору и повторная проверка с ответами.
          Модель в записи не вызывается: ответы взяты из записи живого прогона.
        </SectionHead>
        <div className="demo-grid">
          <figure className="window" data-reveal>
            <div className="term-bar">
              <span className="lights" aria-hidden="true"><i /><i /><i /></span>
              <span>docs/demo/demo.gif</span>
            </div>
            <img src={media("demo.gif")} alt="Запись терминала: запуск агента на requirements.pdf, первая страница отчёта, PDF под паролем, вопросы автору и повтор с ответами" loading="lazy" />
          </figure>
          <div className="chapters" data-reveal>
            <span className="eyebrow">Что в записи</span>
            <ol>
              {DEMO_CHAPTERS.map(([t, s]) => (
                <li key={t}><code>{t}</code><span>{s}</span></li>
              ))}
            </ol>
            <div className="demo-links">
              <a href={media("demo.mp4")}>Видео mp4</a>
              <a href={gh("docs/demo.md")}>Сценарий</a>
              <a href={gh("docs/demo/README.md")}>Как перезаписать</a>
            </div>
            <div className="cmdline sm">
              <span className="ps">$</span><code>bash docs/demo/demo.sh</code>
              <CopyButton text="bash docs/demo/demo.sh" small label="Копировать команду" />
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

function TermLines({ lines }) {
  const all = lines.filter((l) => l.c).map((l) => l.c).join("\n");
  return (
    <div className="term">
      <div className="term-bar">
        <span className="lights" aria-hidden="true"><i /><i /><i /></span>
        <span>terminal</span>
        {lines.filter((l) => l.c).length > 1 && <CopyButton text={all} label="Копировать всё" />}
      </div>
      <div className="term-body">
        {lines.map((l, i) =>
          l.c ? (
            <div className="ln cmd row" key={i}>
              <span className="ps">$</span><code>{l.c}</code>
              <CopyButton text={l.c} small label="Копировать строку" />
            </div>
          ) : (
            <div className="ln cmt" key={i}>{l.k}</div>
          ),
        )}
      </div>
    </div>
  );
}

function Install() {
  const [tab, setTab] = useState(0);
  const tabRefs = useRef([]);
  const onKey = (e, i) => {
    const d = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
    if (!d) return;
    const n = (i + d + PATHS.length) % PATHS.length;
    setTab(n);
    tabRefs.current[n]?.focus();
  };
  const p = PATHS[tab];
  return (
    <section id="install">
      <div className="wrap split install">
        <div className="sticky">
          <SectionHead idx={7} eyebrow="Как запустить" title="Три шага до первого отчёта">
            Нужны git и uv: uv сам скачает Python 3.11+ и все библиотеки. Команды одинаковы в bash и PowerShell.
          </SectionHead>
          <div className="quick" data-reveal>
            <span className="k">Просто посмотреть отчёт?</span>
            <p>Путь А пересобирает наш отчёт без модели и без ключа, за секунды.</p>
            <button type="button" className="link-btn" onClick={() => { setTab(0); document.getElementById("step-3")?.scrollIntoView({ behavior: reduced() ? "auto" : "smooth", block: "start" }); }}>
              К пути А →
            </button>
          </div>
        </div>
        <ol className="stages">
          <li className="stage" data-reveal>
            <span className="badge">1</span>
            <div className="stage-body">
              <h3>Поставьте uv</h3>
              <p className="hint">После установки перезапустите терминал.</p>
              <TermLines lines={[
                { k: "# macOS и Linux" },
                { c: "curl -LsSf https://astral.sh/uv/install.sh | sh" },
                { k: "# Windows (PowerShell)" },
                { c: 'powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"' },
              ]} />
            </div>
          </li>
          <li className="stage" data-reveal>
            <span className="badge">2</span>
            <div className="stage-body">
              <h3>Скачайте агента</h3>
              <TermLines lines={[
                { c: "git clone https://github.com/noncalan/QAIRU-larpers.git" },
                { c: "cd QAIRU-larpers" },
                { c: "uv sync" },
              ]} />
            </div>
          </li>
          <li className="stage" id="step-3" data-reveal>
            <span className="badge">3</span>
            <div className="stage-body">
              <h3>Запустите проверку</h3>
              <p className="hint">Выберите, как агент будет вызывать модель Claude.</p>
              <div className="tabs" role="tablist" aria-label="Способ запуска">
                {PATHS.map((x, i) => (
                  <button
                    key={x.id}
                    ref={(el) => (tabRefs.current[i] = el)}
                    type="button"
                    role="tab"
                    id={`t-${x.id}`}
                    aria-controls={`p-${x.id}`}
                    aria-selected={i === tab}
                    tabIndex={i === tab ? 0 : -1}
                    onClick={() => setTab(i)}
                    onKeyDown={(e) => onKey(e, i)}
                  >
                    {x.tab}
                  </button>
                ))}
              </div>
              <div className="tabpanel" role="tabpanel" id={`p-${p.id}`} aria-labelledby={`t-${p.id}`} key={p.id}>
                <p><b>{p.lead}</b> {p.text}</p>
                <TermLines lines={p.lines} />
              </div>
            </div>
          </li>
        </ol>
      </div>

      <div className="wrap split also-wrap">
        <div>
          <span className="eyebrow" data-reveal>Ещё в CLI</span>
          <div className="also">
            {ALSO.map(([c, t]) => (
              <div key={c} data-reveal>
                <code>{c}</code>
                <p>{t}</p>
              </div>
            ))}
          </div>
        </div>
        <div data-reveal>
          <span className="eyebrow">Что получится рядом с отчётом</span>
          <pre className="tree">
            {OUT_TREE.map(([a, b]) => (
              <div key={a}><span>{a.padEnd(20, " ")}</span>{b && <span className="c"># {b}</span>}</div>
            ))}
          </pre>
        </div>
      </div>
    </section>
  );
}

function Team() {
  return (
    <section id="team">
      <div className="wrap">
        <SectionHead idx={8} eyebrow="Команда" title="QAIRU larpers">
          BCC-HUB x MNU AI Agent Challenge 2026, кейс AI Requirements Validator.
        </SectionHead>
        <div className="team">
          {TEAM.map(([name, img, pos], i) => (
            <figure className="person" key={name} data-reveal style={{ "--d": `${i * 90}ms` }}>
              <div className="portrait"><img src={img} alt={`Пиксельный портрет: ${name}`} loading="lazy" style={{ objectPosition: pos }} /></div>
              <figcaption>
                <b>{name}</b>
                <span>QAIRU larpers · {String(i + 1).padStart(2, "0")}</span>
              </figcaption>
            </figure>
          ))}
        </div>
      </div>
    </section>
  );
}

function Footer() {
  return (
    <footer>
      <div className="wrap foot">
        <div>
          <span className="brand"><Logo size={24} />QAIRU validator</span>
          <p>Модель предлагает, код проверяет, человек решает.</p>
        </div>
        <nav aria-label="Документы">
          <a href={REPO}>README</a>
          <a href={gh("docs/architecture.md")}>Архитектура</a>
          <a href={gh("docs/value.md")}>Ценность</a>
          <a href={gh("docs/demo.md")}>Демо</a>
          <a href={gh("LICENSE")}>MIT</a>
        </nav>
      </div>
    </footer>
  );
}

export default function App() {
  const [active, setActive] = useState("");

  useEffect(() => {
    // появление при прокрутке
    const els = document.querySelectorAll("[data-reveal]");
    if (reduced()) { els.forEach((el) => el.classList.add("in")); return; }
    const io = new IntersectionObserver(
      (entries) => entries.forEach((e) => { if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); } }),
      { threshold: 0.12, rootMargin: "0px 0px -6% 0px" },
    );
    els.forEach((el) => io.observe(el));
    return () => io.disconnect();
  }, []);

  useEffect(() => {
    // активный раздел для строки состояния
    const io = new IntersectionObserver(
      (entries) => entries.forEach((e) => { if (e.isIntersecting) setActive(e.target.id); }),
      { rootMargin: "-40% 0px -55% 0px" },
    );
    document.querySelectorAll("section[id]").forEach((s) => io.observe(s));
    return () => io.disconnect();
  }, []);

  return (
    <>
      <Intro />
      <a className="skip" href="#problem">К содержанию</a>
      <TopNav />
      <main>
        <Hero />
        <Problem />
        <Agent />
        <Pipeline />
        <Quality />
        <Report />
        <Demo />
        <Install />
        <Team />
      </main>
      <Footer />
      <StatusBar active={active} />
    </>
  );
}
