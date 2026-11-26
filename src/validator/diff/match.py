"""Сопоставление двух версий: требования, затем находки, затем классы находок. Без модели.

Порядок для требований: ``stable_key`` → номер и раздел документа → похожесть текста.
Находки сопоставляем по свойству, пересечению цитат и месту в документе.

Главное правило классов: исчезновение находки засчитывается как «исправлено», только если её место в
документе изменилось. Если цитата дословно осталась, находка «пропала без правки»: это нестабильность
модели между прогонами, а не работа аналитика. То же для новых находок: «новое на изменённом месте»
может быть регрессией от правки, «новое на неизменённом месте» это шум.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Iterable, Sequence
from dataclasses import dataclass

from rapidfuzz import fuzz

from validator.diff.load import FindingView, ReportView, ReqView
from validator.diff.model import DiffResult, FindingClass, FindingDiff, MatchHow, ReqPair, ReqStatus
from validator.models import Scope
from validator.text import MIN_FUZZY_CHARS, normalize

REQ_SIMILARITY = 70.0
"""Похожесть текста (rapidfuzz ratio, 0–100), с которой требование новой версии считаем правкой
старого, а не новым требованием. Если меньше: «удалено» и «добавлено»."""

SAME_SOURCE_SIMILARITY = 30.0
"""Мягкая граница похожести для частей одного пункта документа (общий номер и раздел): пункт тот же,
поэтому переписанную фразу считаем правкой, а не новой."""

QUOTE_OVERLAP = 90.0
"""Похожесть цитат (partial_ratio) двух находок, с которой это одно и то же место документа."""

_SEP = "\x00"
_SEVERITY = {"critical": "критично", "major": "важно", "minor": "незначительно"}
_SCORE_TYPE = 1


def _norm(text: str) -> str:
    return normalize(text)[0]


# --- требования -------------------------------------------------------------------------------


def _pair_by_key(
    old: list[ReqView],
    new: list[ReqView],
    key: Callable[[ReqView], Hashable | None],
    how: MatchHow,
    out: list[ReqPair],
) -> tuple[list[ReqView], list[ReqView]]:
    """Пары по значению поля, если оно уникально с обеих сторон; остаток возвращаем."""

    def unique(items: list[ReqView]) -> dict[Hashable, ReqView]:
        seen: dict[Hashable, list[ReqView]] = {}
        for r in items:
            value = key(r)
            if value:
                seen.setdefault(value, []).append(r)
        return {k: v[0] for k, v in seen.items() if len(v) == 1}

    left, right = unique(old), unique(new)
    used_old: set[str] = set()
    used_new: set[str] = set()
    for value, o in left.items():
        n = right.get(value)
        if n is None:
            continue
        same = _norm(o.quote) == _norm(n.quote)
        out.append(ReqPair(ReqStatus.UNCHANGED if same else ReqStatus.CHANGED, o, n, how))
        used_old.add(o.id)
        used_new.add(n.id)
    return [r for r in old if r.id not in used_old], [r for r in new if r.id not in used_new]


def _pair_greedy(
    old: list[ReqView], new: list[ReqView], minimum: float, how: MatchHow, out: list[ReqPair]
) -> tuple[list[ReqView], list[ReqView]]:
    """Пары по похожести текста не ниже ``minimum``, от самых похожих; остаток возвращаем."""
    scored = sorted(
        (
            (fuzz.ratio(_norm(o.quote), _norm(n.quote)), i, j)
            for i, o in enumerate(old)
            for j, n in enumerate(new)
        ),
        key=lambda t: (-t[0], t[1], t[2]),
    )
    used_old: set[int] = set()
    used_new: set[int] = set()
    for score, i, j in scored:
        if score < minimum:
            break
        if i in used_old or j in used_new:
            continue
        used_old.add(i)
        used_new.add(j)
        o, n = old[i], new[j]
        same = _norm(o.quote) == _norm(n.quote)
        status = ReqStatus.UNCHANGED if same else ReqStatus.CHANGED
        out.append(ReqPair(status, o, n, how, round(score, 1)))
    rest_old = [o for i, o in enumerate(old) if i not in used_old]
    rest_new = [n for j, n in enumerate(new) if j not in used_new]
    return rest_old, rest_new


def _pair_within_source(
    old: list[ReqView], new: list[ReqView], out: list[ReqPair]
) -> tuple[list[ReqView], list[ReqView]]:
    """Части одного пункта документа (общий номер и раздел): пары по похожести с мягкой границей."""
    groups: dict[tuple[str, tuple[str, ...]], tuple[list[ReqView], list[ReqView]]] = {}
    for side, items in enumerate((old, new)):
        for r in items:
            if (key := _source_key(r)) is not None:
                groups.setdefault(key, ([], []))[side].append(r)
    paired: set[tuple[int, str]] = set()
    for left, right in groups.values():
        before = len(out)
        _pair_greedy(left, right, SAME_SOURCE_SIMILARITY, MatchHow.SOURCE_ID, out)
        for pair in out[before:]:
            assert pair.old is not None and pair.new is not None  # noqa: S101
            paired.add((0, pair.old.id))
            paired.add((1, pair.new.id))
    return [r for r in old if (0, r.id) not in paired], [r for r in new if (1, r.id) not in paired]


def _source_key(req: ReqView) -> tuple[str, tuple[str, ...]] | None:
    return (req.source_id, tuple(map(_norm, req.section_path))) if req.source_id else None


def match_requirements(old: Sequence[ReqView], new: Sequence[ReqView]) -> list[ReqPair]:
    """Пары требований старой и новой версии, по порядку старой версии, добавленные в конце."""
    out: list[ReqPair] = []
    rest_old, rest_new = _pair_by_key(list(old), list(new), lambda r: r.stable_key, MatchHow.STABLE_KEY, out)
    rest_old, rest_new = _pair_by_key(rest_old, rest_new, _source_key, MatchHow.SOURCE_ID, out)
    rest_old, rest_new = _pair_within_source(rest_old, rest_new, out)
    rest_old, rest_new = _pair_greedy(rest_old, rest_new, REQ_SIMILARITY, MatchHow.SIMILARITY, out)
    out.extend(ReqPair(ReqStatus.REMOVED, o, None) for o in rest_old)
    out.extend(ReqPair(ReqStatus.ADDED, None, n) for n in rest_new)
    old_order = {r.id: i for i, r in enumerate(old)}
    new_order = {r.id: i for i, r in enumerate(new)}

    def order(p: ReqPair) -> tuple[int, int]:
        if p.old is not None:
            return (old_order[p.old.id], 0)
        assert p.new is not None  # noqa: S101
        return (len(old_order) + new_order[p.new.id], 1)

    return sorted(out, key=order)


# --- место в документе -----------------------------------------------------------------------


@dataclass(frozen=True)
class Places:
    """Где искать цитаты: тексты версий документа или, без них, цитаты требований из отчётов."""

    old_text: str
    new_text: str
    from_documents: bool
    same_document: bool

    def changed(self, quote: str) -> bool | None:
        """Место цитаты старой версии изменилось в новой: True; осталось дословно: False; не знаем: None."""
        q = _norm(quote)
        if not q or q not in self.old_text:
            return None
        return q not in self.new_text

    def appeared(self, quote: str) -> bool | None:
        """Цитата новой версии появилась правкой (её нет в старой): True; была и раньше: False."""
        q = _norm(quote)
        if not q or q not in self.new_text:
            return None
        return q not in self.old_text


def places(
    old: ReportView, new: ReportView, old_text: str | None = None, new_text: str | None = None
) -> Places:
    old_sha = old.passport.document_sha256
    same_document = bool(old_sha and old_sha == new.passport.document_sha256)
    if old_text is not None and new_text is not None:
        return Places(_norm(old_text), _norm(new_text), True, same_document)
    corpus_old = _SEP.join(_norm(r.quote) for r in old.requirements)
    corpus_new = _SEP.join(_norm(r.quote) for r in new.requirements)
    return Places(corpus_old, corpus_new, False, same_document)


# --- находки ----------------------------------------------------------------------------------


def _quotes_overlap(a: FindingView, b: FindingView) -> bool:
    for qa in a.quotes:
        na = _norm(qa.text)
        for qb in b.quotes:
            nb = _norm(qb.text)
            if not na or not nb:
                continue
            if na in nb or nb in na:
                return True
            if min(len(na), len(nb)) >= MIN_FUZZY_CHARS and fuzz.partial_ratio(na, nb) >= QUOTE_OVERLAP:
                return True
    return False


def _section(finding: FindingView, sections: dict[str, tuple[str, ...]]) -> tuple[str, ...] | None:
    if finding.section_path:
        return tuple(map(_norm, finding.section_path))
    if not finding.requirement_ids:
        return ()
    linked = {sections.get(rid) for rid in finding.requirement_ids}
    return next(iter(linked)) if len(linked) == 1 else None


def _score(
    old: FindingView,
    new: FindingView,
    req_map: dict[str, str],
    old_section: tuple[str, ...] | None,
    new_section: tuple[str, ...] | None,
) -> int:
    if old.property != new.property or old.scope != new.scope:
        return 0
    if old.scope == Scope.REQUIREMENT:
        mapped = {req_map[rid] for rid in old.requirement_ids if rid in req_map}
        same_place = bool(mapped.intersection(new.requirement_ids))
    elif old.scope == Scope.SET:
        same_place = old_section is not None and old_section == new_section
    else:
        return 0
    if not same_place or not _quotes_overlap(old, new):
        return 0
    return 1 + _SCORE_TYPE * (old.defect_type == new.defect_type)


def _greedy_pairs(scored: list[tuple[int, int, int]]) -> list[tuple[int, int]]:
    pairs: list[tuple[int, int]] = []
    used_old: set[int] = set()
    used_new: set[int] = set()
    for _, i, j in sorted(scored, key=lambda t: (-t[0], t[1], t[2])):
        if i in used_old or j in used_new:
            continue
        used_old.add(i)
        used_new.add(j)
        pairs.append((i, j))
    return pairs


def _pair_findings(
    old: ReportView, new: ReportView, req_map: dict[str, str], same_document: bool
) -> list[tuple[int, int]]:
    # В копиях одного анализа могут повторяться находки без цитат или ссылок.
    if old == new or (
        same_document and old.requirements == new.requirements and old.findings == new.findings
    ):
        return [(i, i) for i in range(len(old.findings))]

    def sections(report: ReportView) -> list[tuple[str, ...] | None]:
        by_id = {r.id: tuple(map(_norm, r.section_path)) for r in report.requirements}
        return [_section(finding, by_id) for finding in report.findings]

    old_sections, new_sections = sections(old), sections(new)
    return _greedy_pairs(
        [
            (score, i, j)
            for i, before in enumerate(old.findings)
            for j, after in enumerate(new.findings)
            if (score := _score(before, after, req_map, old_sections[i], new_sections[j]))
        ]
    )


def _short(text: str, limit: int = 80) -> str:
    one = " ".join(text.split())
    return one if len(one) <= limit else one[: limit - 1] + "…"


def _proven_edit(quote: str, finding: FindingView, pairs: dict[str, ReqPair], old_side: bool) -> bool:
    """В отчётах без документов сверяем цитату с обоими фрагментами одного требования."""
    normalized = _norm(quote)
    for rid in finding.requirement_ids:
        pair = pairs.get(rid)
        if pair is None or pair.status is not ReqStatus.CHANGED or pair.old is None or pair.new is None:
            continue
        source = pair.old if old_side else pair.new
        counterpart = pair.new if old_side else pair.old
        if normalized and normalized in _norm(source.quote) and normalized not in _norm(counterpart.quote):
            return True
    return False


def _classify_unmatched(
    finding: FindingView, where: Places, pairs: dict[str, ReqPair], *, old_side: bool
) -> FindingDiff:
    old = finding if old_side else None
    new = None if old_side else finding
    edited_class = FindingClass.FIXED if old_side else FindingClass.NEW_CHANGED
    other_class = FindingClass.VANISHED if old_side else FindingClass.NEW_UNCHANGED

    if where.same_document:
        return FindingDiff(other_class, old, new, "хеш документа совпадает, место не менялось")

    check = where.changed if old_side else where.appeared
    confirmed_unchanged = False
    for quote in finding.quotes:
        verdict = check(quote.text)
        if verdict is False:
            confirmed_unchanged = True
        if verdict and (where.from_documents or _proven_edit(quote.text, finding, pairs, old_side)):
            return FindingDiff(edited_class, old, new, f"место цитаты изменено: «{_short(quote.text)}»")
    if confirmed_unchanged:
        return FindingDiff(other_class, old, new, "цитата есть в обеих версиях")
    return FindingDiff(
        FindingClass.PLACE_UNKNOWN, old, new, "по доступным данным изменение места находки не подтверждено"
    )


def _remained(o: FindingView, n: FindingView, req_status: dict[str, ReqStatus]) -> FindingDiff:
    """Осталось. Если требование правили, а находка не ушла, говорим об этом: правка не помогла."""
    notes: list[str] = []
    edited = [r for r in o.requirement_ids if req_status.get(r) is ReqStatus.CHANGED]
    if edited:
        notes.append(f"требование {', '.join(edited)} изменено, но находка не закрыта")
    if o.severity != n.severity:
        notes.append(
            f"важность {_SEVERITY.get(o.severity, o.severity)} → {_SEVERITY.get(n.severity, n.severity)}"
        )
    return FindingDiff(FindingClass.REMAINED, o, n, "; ".join(notes))


def match_findings(
    old: ReportView, new: ReportView, pairs: Iterable[ReqPair], where: Places
) -> list[FindingDiff]:
    pairs = list(pairs)
    req_map = {p.old.id: p.new.id for p in pairs if p.old is not None and p.new is not None}
    old_pairs = {p.old.id: p for p in pairs if p.old is not None}
    new_pairs = {p.new.id: p for p in pairs if p.new is not None}
    old_status = {} if where.same_document else {rid: p.status for rid, p in old_pairs.items()}
    matched = _pair_findings(old, new, req_map, where.same_document)
    out = [_remained(old.findings[i], new.findings[j], old_status) for i, j in matched]
    used_old = {i for i, _ in matched}
    used_new = {j for _, j in matched}
    out.extend(
        _classify_unmatched(f, where, old_pairs, old_side=True)
        for i, f in enumerate(old.findings)
        if i not in used_old
    )
    out.extend(
        _classify_unmatched(f, where, new_pairs, old_side=False)
        for j, f in enumerate(new.findings)
        if j not in used_new
    )
    return out


# --- целиком ----------------------------------------------------------------------------------


def _warnings(old: ReportView, new: ReportView, where: Places) -> list[str]:
    a, b = old.passport, new.passport
    out: list[str] = []
    if a.agent_version != b.agent_version or a.prompts_sha256 != b.prompts_sha256:
        out.append(
            f"Отчёты сделаны разными версиями агента ({a.agent_version or '?'} и {b.agent_version or '?'}"
            + (", разные промпты" if a.prompts_sha256 != b.prompts_sha256 else "")
            + "): часть разницы может быть от агента, а не от правок документа."
        )
    if (
        a.models != b.models
        or set(a.models_actual) != set(b.models_actual)
        or (a.profile, a.passes) != (b.profile, b.passes)
    ):
        out.append(
            "Отчёты сделаны с разными моделями или настройками прогона (профиль, число проходов): "
            "часть разницы может быть от этого, а не от правок."
        )
    if where.same_document:
        out.append("Хеш документа в обоих отчётах одинаковый. Различия отчётов не отражают правки документа.")
    if not where.from_documents:
        out.append(
            "Тексты документов не переданы (--old-doc, --new-doc). Правки сверены по цитатам "
            "требований из отчётов. Цитаты вне требований (примеры, таблицы) так не сверить."
        )
    for view in (old, new):
        if view.status and view.status != "complete":
            out.append(f"Отчёт {view.path}: проверка выполнена не полностью ({view.status}).")
    return out


def compare(
    old: ReportView, new: ReportView, old_text: str | None = None, new_text: str | None = None
) -> DiffResult:
    """Сравнение двух отчётов (и, по желанию, текстов версий документа). Детерминированно."""
    where = places(old, new, old_text, new_text)
    reqs = match_requirements(old.requirements, new.requirements)
    findings = match_findings(old, new, reqs, where)
    return DiffResult(
        old=old,
        new=new,
        requirements=tuple(reqs),
        findings=tuple(findings),
        warnings=tuple(_warnings(old, new, where)),
        texts=where.from_documents,
    )
