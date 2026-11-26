"""Результат сравнения двух версий документа: требования, находки и их классы."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from validator.diff.load import FindingView, ReportView, ReqView


class ReqStatus(StrEnum):
    UNCHANGED = "unchanged"
    CHANGED = "changed"
    REMOVED = "removed"
    ADDED = "added"

    @property
    def label(self) -> str:
        return _REQ_LABELS[self]


_REQ_LABELS = {
    ReqStatus.UNCHANGED: "без изменений",
    ReqStatus.CHANGED: "изменено",
    ReqStatus.REMOVED: "удалено",
    ReqStatus.ADDED: "добавлено",
}


class MatchHow(StrEnum):
    STABLE_KEY = "stable_key"
    SOURCE_ID = "source_id"
    SIMILARITY = "similarity"
    NONE = "none"

    @property
    def label(self) -> str:
        return _HOW_LABELS[self]


_HOW_LABELS = {
    MatchHow.STABLE_KEY: "текст тот же",
    MatchHow.SOURCE_ID: "по номеру в документе",
    MatchHow.SIMILARITY: "по похожести текста",
    MatchHow.NONE: "—",
}


class FindingClass(StrEnum):
    """Класс находки при сравнении версий (docs/version-diff.md)."""

    FIXED = "fixed"
    VANISHED = "vanished_without_edit"
    REMAINED = "remained"
    NEW_CHANGED = "new_on_changed_place"
    NEW_UNCHANGED = "new_on_unchanged_place"

    @property
    def label(self) -> str:
        return _CLASS_LABELS[self]


_CLASS_LABELS = {
    FindingClass.FIXED: "исправлено",
    FindingClass.VANISHED: "пропала без правки",
    FindingClass.REMAINED: "осталось",
    FindingClass.NEW_CHANGED: "новое на изменённом месте",
    FindingClass.NEW_UNCHANGED: "новое на неизменённом месте",
}


@dataclass(frozen=True)
class ReqPair:
    status: ReqStatus
    old: ReqView | None
    new: ReqView | None
    how: MatchHow = MatchHow.NONE
    score: float | None = None


@dataclass(frozen=True)
class FindingDiff:
    cls: FindingClass
    old: FindingView | None
    new: FindingView | None
    reason: str = ""

    @property
    def main(self) -> FindingView:
        """Находка, о которой строка: новая, если есть, иначе старая."""
        found = self.new or self.old
        assert found is not None  # noqa: S101 - пара без находок не создаётся
        return found


@dataclass(frozen=True)
class DiffResult:
    old: ReportView
    new: ReportView
    requirements: tuple[ReqPair, ...]
    findings: tuple[FindingDiff, ...]
    warnings: tuple[str, ...] = ()
    texts: bool = False
    """Сравнение видело тексты обеих версий документа (точнее), а не только отчёты."""

    def of(self, cls: FindingClass) -> list[FindingDiff]:
        return [d for d in self.findings if d.cls is cls]

    def reqs(self, status: ReqStatus) -> list[ReqPair]:
        return [p for p in self.requirements if p.status is status]
