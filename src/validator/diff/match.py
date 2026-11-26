"""Сопоставление двух версий: требования, затем находки, затем классы находок. Без модели.

Порядок для требований: ``stable_key`` (текст не менялся) → номер в документе (``source_id``) →
похожесть текста (rapidfuzz) не ниже ``REQ_SIMILARITY``. Находки сопоставляем по свойству и месту:
общее требование (через пары требований) или общая цитата после ``normalize``.

Главное правило классов: исчезновение находки засчитывается как «исправлено», только если её место в
документе изменилось. Если цитата дословно осталась, находка «пропала без правки»: это нестабильность
модели между прогонами, а не работа аналитика. То же для новых находок: «новое на изменённом месте»
может быть регрессией от правки, «новое на неизменённом месте» это шум.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from rapidfuzz import fuzz

from validator.diff.load import FindingView, ReportView, ReqView
from validator.diff.model import DiffResult, FindingClass, FindingDiff, MatchHow, ReqPair, ReqStatus
from validator.text import MIN_FUZZY_CHARS, normalize

REQ_SIMILARITY = 70.0
"""Похожесть текста (rapidfuzz ratio, 0–100), с которой требование новой версии считаем правкой
старого, а не новым требованием. Если меньше: «удалено» и «добавлено»."""

QUOTE_OVERLAP = 90.0
"""Похожесть цитат (partial_ratio) двух находок, с которой это одно и то же место документа."""

_SEP = "\x00"
_SEVERITY = {"critical": "критично", "major": "важно", "minor": "незначительно"}
_SCORE_REQ = 2
_SCORE_QUOTE = 3
_SCORE_TYPE = 1
_SCORE_SECTION = 1


def _norm(text: str) -> str:
    return normalize(text)[0]


# --- требования -------------------------------------------------------------------------------


def _pair_by_key(
    old: list[ReqView], new: list[ReqView], key: str, how: MatchHow, out: list[ReqPair]
) -> tuple[list[ReqView], list[ReqView]]:
    """Пары по значению поля, если оно уникально с обеих сторон; остаток возвращаем."""

    def unique(items: list[ReqView]) -> dict[str, ReqView]:
        seen: dict[str, list[ReqView]] = {}
        for r in items:
            value = getattr(r, key)
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


def _pair_by_similarity(old: list[ReqView], new: list[ReqView], out: list[ReqPair]) -> None:
    scored = sorted(
        (
            (fuzz.ratio(_norm(o.quote), _norm(n.quote)), i, j)
            for i, o in enumerate(old)
            for j, n in enumerate(new)
        ),
        reverse=True,
    )
    used_old: set[int] = set()
    used_new: set[int] = set()
    for score, i, j in scored:
        if score < REQ_SIMILARITY:
            break
        if i in used_old or j in used_new:
            continue
        used_old.add(i)
        used_new.add(j)
        o, n = old[i], new[j]
        same = _norm(o.quote) == _norm(n.quote)
        status = ReqStatus.UNCHANGED if same else ReqStatus.CHANGED
        out.append(ReqPair(status, o, n, MatchHow.SIMILARITY, round(score, 1)))
    out.extend(ReqPair(ReqStatus.REMOVED, o, None) for i, o in enumerate(old) if i not in used_old)
    out.extend(ReqPair(ReqStatus.ADDED, None, n) for j, n in enumerate(new) if j not in used_new)


def match_requirements(old: Sequence[ReqView], new: Sequence[ReqView]) -> list[ReqPair]:
    """Пары требований старой и новой версии, по порядку старой версии, добавленные в конце."""
    out: list[ReqPair] = []
    rest_old, rest_new = _pair_by_key(list(old), list(new), "stable_key", MatchHow.STABLE_KEY, out)
    rest_old, rest_new = _pair_by_key(rest_old, rest_new, "source_id", MatchHow.SOURCE_ID, out)
    _pair_by_similarity(rest_old, rest_new, out)
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
    if old_text is not None and new_text is not None:
        return Places(_norm(old_text), _norm(new_text), True)
    corpus_old = _SEP.join(_norm(r.quote) for r in old.requirements)
    corpus_new = _SEP.join(_norm(r.quote) for r in new.requirements)
    return Places(corpus_old, corpus_new, False)


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


def _score(o: FindingView, n: FindingView, req_map: dict[str, str]) -> int:
    """Насколько старая и новая находка про одно и то же; 0 значит не пара."""
    if o.property != n.property:
        return 0
    mapped = {req_map[r] for r in o.requirement_ids if r in req_map}
    by_req = bool(mapped & set(n.requirement_ids))
    by_quote = _quotes_overlap(o, n)
    if not by_req and not by_quote:
        return 0
    score = _SCORE_REQ * by_req + _SCORE_QUOTE * by_quote
    score += _SCORE_TYPE * (o.defect_type == n.defect_type)
    score += _SCORE_SECTION * (bool(o.section_path) and o.section_path == n.section_path)
    return score


def _pair_findings(
    old: Sequence[FindingView], new: Sequence[FindingView], req_map: dict[str, str]
) -> list[tuple[int, int]]:
    scored = sorted(
        ((s, i, j) for i, o in enumerate(old) for j, n in enumerate(new) if (s := _score(o, n, req_map))),
        key=lambda t: (-t[0], t[1], t[2]),
    )
    pairs: list[tuple[int, int]] = []
    used_old: set[int] = set()
    used_new: set[int] = set()
    for _, i, j in scored:
        if i in used_old or j in used_new:
            continue
        used_old.add(i)
        used_new.add(j)
        pairs.append((i, j))
    return pairs


def _short(text: str, limit: int = 80) -> str:
    one = " ".join(text.split())
    return one if len(one) <= limit else one[: limit - 1] + "…"


def _touched(f: FindingView, req_status: dict[str, ReqStatus], statuses: tuple[ReqStatus, ...]) -> str:
    touched = [r for r in f.requirement_ids if req_status.get(r) in statuses]
    return ", ".join(f"{r} {req_status[r].label}" for r in touched)


def _classify_gone(f: FindingView, where: Places, req_status: dict[str, ReqStatus]) -> FindingDiff:
    """Находки нет в новой версии: исправлено, если её место правили; иначе пропала без правки.

    Решают цитаты. Требования смотрим, только если ни одну цитату не удалось найти в старой версии:
    цитата, которая дословно осталась, сильнее того, что соседнюю фразу требования правили.
    """
    verdicts = [where.changed(q.text) for q in f.quotes]
    for q, changed in zip(f.quotes, verdicts, strict=True):
        if changed:
            return FindingDiff(FindingClass.FIXED, f, None, f"место цитаты изменено: «{_short(q.text)}»")
    labels = _touched(f, req_status, (ReqStatus.CHANGED, ReqStatus.REMOVED))
    if labels and all(v is None for v in verdicts):
        return FindingDiff(FindingClass.FIXED, f, None, f"требование {labels}")
    return FindingDiff(
        FindingClass.VANISHED, f, None, "цитата дословно осталась в новой версии, а находки нет"
    )


def _classify_new(f: FindingView, where: Places, req_status: dict[str, ReqStatus]) -> FindingDiff:
    """Новая находка: на изменённом месте (возможная регрессия) или на неизменённом (шум модели)."""
    verdicts = [where.appeared(q.text) for q in f.quotes]
    for q, appeared in zip(f.quotes, verdicts, strict=True):
        if appeared:
            return FindingDiff(FindingClass.NEW_CHANGED, None, f, f"цитата из правки: «{_short(q.text)}»")
    labels = _touched(f, req_status, (ReqStatus.CHANGED, ReqStatus.ADDED))
    if labels and all(v is None for v in verdicts):
        return FindingDiff(FindingClass.NEW_CHANGED, None, f, f"требование {labels}")
    return FindingDiff(
        FindingClass.NEW_UNCHANGED, None, f, "место не менялось: в старой версии модель это не отметила"
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
    old_status = {p.old.id: p.status for p in pairs if p.old is not None}
    new_status = {p.new.id: p.status for p in pairs if p.new is not None}
    matched = _pair_findings(old.findings, new.findings, req_map)
    out = [_remained(old.findings[i], new.findings[j], old_status) for i, j in matched]
    used_old = {i for i, _ in matched}
    used_new = {j for _, j in matched}
    out += [_classify_gone(f, where, old_status) for i, f in enumerate(old.findings) if i not in used_old]
    out += [_classify_new(f, where, new_status) for j, f in enumerate(new.findings) if j not in used_new]
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
    if a.models != b.models or (a.profile, a.passes) != (b.profile, b.passes):
        out.append(
            "Отчёты сделаны с разными моделями или настройками прогона (профиль, число проходов): "
            "часть разницы может быть от этого, а не от правок."
        )
    if a.document_sha256 and a.document_sha256 == b.document_sha256:
        out.append("Хеш документа в обоих отчётах одинаковый: документ не менялся, вся разница от модели.")
    if not where.from_documents:
        out.append(
            "Тексты документов не переданы (--old-doc, --new-doc): изменённые места определены по цитатам "
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
