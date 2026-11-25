"""Выдуманные отчёты для тестов отчёта: документ про бронирование переговорных, не наш и не эталонный.

``uv run python tests/report_factory.py`` пересобирает ``tests/fixtures/report/*.report.json``.
Эталонные ``*.expected.md`` обновляются через ``UPDATE_SNAPSHOTS=1 uv run pytest tests/test_report_md.py``
после того, как вы глазами проверили изменение отчёта.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from validator.models import (
    Addressee,
    Anchor,
    Backend,
    Conclusion,
    DiagLevel,
    Diagnostic,
    DiscardedFinding,
    DiscardReason,
    DocKind,
    DocumentInfo,
    ExitCode,
    Extraction,
    Finding,
    FindingSource,
    MergedFinding,
    Passport,
    Profile,
    Property,
    Question,
    QuestionOption,
    QuestionPriority,
    Quote,
    QuoteMatch,
    Report,
    ReportStatus,
    Requirement,
    RequirementType,
    RunMetrics,
    Scope,
    Segmentation,
    SegmentationMode,
    Severity,
    SourceKind,
    TokenUsage,
    TrafficLight,
    Verdict,
    Verification,
    WorkStage,
)
from validator.models import DefectType as D
from validator.report.build import FIRST_PAGE_ITEMS
from validator.verify import summarize

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "report"
STARTED = datetime(2026, 1, 15, 9, 30, 0, tzinfo=UTC)


def passport(**changes: object) -> Passport:
    base = {
        "document_name": "rooms.md",
        "document_kind": DocKind.MD,
        "document_sha256": "ab" * 32,
        "document_size_bytes": 2048,
        "started_at": STARTED,
        "finished_at": STARTED.replace(minute=34),
        "duration_s": 240.5,
        "agent_version": "v0.9.0",
        "agent_version_source": "git",
        "prompts_sha256": "cd" * 32,
        "os": "Linux 6.1 (x86_64)",
        "python": "3.12.0",
        "backend_requested": Backend.CLI,
        "backend_used": Backend.CLI,
        "profile": Profile.STANDARD,
        "passes": 3,
        "models": {"search": "claude-opus-5-5 · medium", "questions": "claude-opus-5-5 · high"},
        "models_actual": ["claude-opus-5-5"],
        "calls_total": 5,
        "calls_failed": 0,
        "tokens": TokenUsage(input_tokens=30_000, output_tokens=12_000),
        "cost_usd": 1.62,
        "exit_code": ExitCode.OK,
    }
    return Passport.model_validate({**base, **changes})


def _anchor(start: int, length: int, line: int, **extra: object) -> Anchor:
    return Anchor(char_start=start, char_end=start + length, line_start=line, line_end=line, **extra)  # type: ignore[arg-type]


def _req(
    n: int, source_id: str | None, quote: str, start: int, line: int, section: list[str], **extra: object
) -> Requirement:
    return Requirement(
        id=f"R-{n:02d}",
        source_id=source_id,
        source_locator="" if source_id else f"{section[-1]}, п. {n}",
        section_path=section,
        type=RequirementType.FUNCTIONAL,
        quote=quote,
        summary=quote[:40],
        anchor=_anchor(start, len(quote), line, **extra),
        stable_key=f"key{n:02d}",
    )


_BOOKING = ["Бронирование"]
_LIMITS = ["Ограничения", "Нагрузка"]
REQS = [
    _req(1, "ФТ-1", "Пользователь бронирует комнату на выбранное время.", 100, 4, _BOOKING),
    _req(2, "ФТ-2", "Бронь можно отменить за час до начала.", 160, 5, _BOOKING),
    _req(3, None, "Система быстро показывает свободные комнаты.", 210, 6, _BOOKING),
    _req(
        4,
        "НФТ-1",
        "Страница списка комнат открывается не дольше 2 с | при 100 пользователях.",
        300,
        12,
        _LIMITS,
    ),
    _req(5, "НФТ-2", "Пересчёт занятости выполняется по возможности ночью.", 380, 13, _LIMITS),
    _req(6, None, "Администратор добавляет и удаляет комнаты, а также меняет их цену.", 450, 14, _LIMITS),
]


def _quote(text: str, start: int, line: int, match: QuoteMatch = QuoteMatch.EXACT, **extra: object) -> Quote:
    return Quote(
        text=text,
        match=match,
        score=100.0 if match is QuoteMatch.EXACT else 93.0,
        located=text,
        anchor=_anchor(start, len(text), line, **extra),
    )


def _finding(n: int, **fields: object) -> Finding:
    base = {
        "id": f"F-{n:02d}",
        "merged_id": f"M-{n:03d}",
        "scope": Scope.REQUIREMENT,
        "verdict": Verdict.VIOLATION,
        "found_in": 3,
        "of": 3,
        "sources": [FindingSource(kind=SourceKind.PASS, pass_index=i) for i in (1, 2, 3)],
    }
    return Finding.model_validate({**base, **fields})


def findings() -> list[Finding]:
    return [
        _finding(
            1,
            property=Property.SET_CONSISTENT,
            defect_type=D.CONTRADICTION,
            scope=Scope.SET,
            severity=Severity.CRITICAL,
            blocks_testing=True,
            quotes=[
                _quote(REQS[1].quote, 160, 5),
                _quote("Отмена брони возможна в любое время до начала.", 700, 31),
            ],
            requirement_ids=["R-02"],
            explanation="Правило отмены в двух местах задаёт разный срок: за час и в любое время.",
            suggestion="Отмена брони возможна не позднее чем за [уточнить: срок] до начала.",
        ),
        _finding(
            2,
            property=Property.VERIFIABLE,
            defect_type=D.UNVERIFIABLE,
            severity=Severity.MAJOR,
            blocks_testing=True,
            quotes=[_quote("быстро", 227, 6, QuoteMatch.FUZZY)],
            requirement_ids=["R-03"],
            explanation="Слово «быстро» не задаёт измеримого порога: тест нельзя написать.",
            suggestion="Список комнат показывается не позднее чем через [уточнить: N] с.",
            found_in=2,
            sources=[FindingSource(kind=SourceKind.PASS, pass_index=i) for i in (1, 3)],
        ),
        _finding(
            3,
            property=Property.UNAMBIGUOUS,
            defect_type=D.AMBIGUOUS,
            verdict=Verdict.UNCLEAR,
            severity=Severity.MAJOR,
            quotes=[_quote("по возможности ночью", 416, 13)],
            requirement_ids=["R-05"],
            explanation="Неясно, что считать «возможностью» и какое время считается ночью.",
            found_in=1,
            sources=[FindingSource(kind=SourceKind.PASS, pass_index=2)],
        ),
        _finding(
            4,
            property=Property.SINGULAR,
            defect_type=D.NOT_ATOMIC,
            severity=Severity.MINOR,
            quotes=[_quote("добавляет и удаляет комнаты, а также меняет их цену", 463, 14, page=3)],
            requirement_ids=["R-06"],
            explanation="В одном требовании три действия: проверка каждого идёт отдельным тестом.",
            suggestion="Разделить на три требования: добавление, удаление, изменение цены.",
            sources=[FindingSource(kind=SourceKind.LINT, rule_id="LNT-04")],
            found_in=1,
            of=1,
            notes=["Найдено правилом линтера."],
        ),
        _finding(
            5,
            property=Property.SET_COMPLETE,
            defect_type=D.INCOMPLETE,
            scope=Scope.SET,
            severity=Severity.MAJOR,
            quotes=[_quote("Права ролей: TBD", 880, 40, extraction=Extraction.OCR)],
            section_path=["Ограничения", "Права"],
            explanation="Раздел прав ролей не заполнен.",
            suggestion="Описать права ролей: [уточнить: роли и разрешённые действия].",
            found_in=2,
            sources=[FindingSource(kind=SourceKind.PASS, pass_index=i) for i in (1, 2)],
        ),
    ]


def questions() -> list[Question]:
    return [
        Question(
            id="Q-01",
            priority=QuestionPriority.P0,
            blocks=[WorkStage.DEVELOPMENT, WorkStage.TESTING],
            addressee=[Addressee.BUSINESS, Addressee.ANALYST],
            topic="срок отмены",
            quotes=[_quote(REQS[1].quote, 160, 5)],
            question="За сколько до начала можно отменить бронь: за час или в любое время?",
            why="Две реализации разойдутся, один из двух тестов упадёт.",
            options=[
                QuestionOption(label="A", text="Не позднее чем за час", change="Строка про «любое время»"),
                QuestionOption(label="B", text="В любое время до начала", change="ФТ-2"),
            ],
            if_unanswered="Команда возьмёт вариант A (так написано в ФТ-2).",
            related_findings=["F-01"],
        ),
        Question(
            id="Q-02",
            priority=QuestionPriority.P1,
            blocks=[WorkStage.TESTING],
            addressee=[Addressee.ANALYST],
            question="Какой порог времени считать быстрым ответом списка комнат?",
            options=[QuestionOption(label="A", text="2 с на 100 пользователях")],
            related_findings=["F-02"],
        ),
        Question(
            id="Q-03",
            priority=QuestionPriority.P2,
            addressee=[Addressee.ARCHITECT],
            question="Какое окно считается ночью для пересчёта занятости?",
            quotes=[_quote("по возможности ночью", 416, 13)],
            options=[
                QuestionOption(label="A", text="С 00:00 до 05:00 по Астане"),
                QuestionOption(label="B", text="Любое окно без нагрузки", change="НФТ-2"),
            ],
            related_findings=["F-03"],
        ),
    ]


def _verification(
    found: list[Finding], reqs: list[Requirement], light: TrafficLight, reason: str
) -> Verification:
    seg = Segmentation(requirements=reqs)
    dropped = [
        DiscardedFinding(
            finding=MergedFinding(
                mid="M-009",
                property=Property.VERIFIABLE,
                defect_type=D.UNVERIFIABLE,
                explanation="Выдуманная цитата.",
            ),
            reason=DiscardReason.QUOTE_NOT_FOUND,
        )
    ]
    status = {r.id: [f.id for f in found if r.id in f.requirement_ids] for r in reqs}
    return Verification(
        findings=found,
        discarded=dropped if found else [],
        summary=summarize(found, dropped if found else [], seg),
        traffic_light=light,
        traffic_reason=reason,
        requirement_status=status,
    )


def _conclusion(ver: Verification, qs: list[Question]) -> Conclusion:
    violations = [f for f in ver.findings if f.verdict is Verdict.VIOLATION]
    violations.sort(key=lambda f: f.severity.rank)
    blocking = [q.id for q in qs if q.priority is not QuestionPriority.P2]
    light = ver.traffic_light
    return Conclusion(
        traffic_light=light,
        headline=f"{light.label.capitalize()}: {ver.traffic_reason}",
        fix_first=[f.id for f in violations[:FIRST_PAGE_ITEMS]],
        ask_first=blocking[:FIRST_PAGE_ITEMS],
    )


def _metrics(ver: Verification, pp: Passport) -> RunMetrics:
    s = ver.summary
    verified = s.quotes_exact + s.quotes_fuzzy
    return RunMetrics(
        duration_s=pp.duration_s,
        calls_total=pp.calls_total,
        calls_failed=pp.calls_failed,
        tokens=pp.tokens,
        cost_usd=pp.cost_usd,
        candidates_total=s.findings_total + s.discarded_total + 2,
        merged_total=s.findings_total + s.discarded_total,
        findings_total=s.findings_total,
        discarded_total=s.discarded_total,
        quotes_total=s.quotes_total,
        quotes_verified=verified,
        verified_quote_share=round(verified / s.quotes_total, 4) if s.quotes_total else None,
    )


def _report(
    ver: Verification,
    qs: list[Question],
    reqs: list[Requirement],
    *,
    status: ReportStatus = ReportStatus.COMPLETE,
    input_diagnostics: list[Diagnostic] | None = None,
    diagnostics: list[Diagnostic] | None = None,
    pass_: Passport | None = None,
) -> Report:
    pp = pass_ or passport()
    return Report(
        status=status,
        passport=pp,
        document=DocumentInfo(
            name=pp.document_name,
            path=f"/tmp/{pp.document_name}",
            kind=pp.document_kind,
            sha256=pp.document_sha256,
            size_bytes=pp.document_size_bytes,
            chars=1300,
            blocks=44,
            extraction="Markdown, построчно",
        ),
        input_diagnostics=input_diagnostics or [],
        diagnostics=diagnostics or [],
        segmentation=Segmentation(requirements=reqs, mode=SegmentationMode.MODEL),
        verification=ver,
        questions=qs,
        conclusion=_conclusion(ver, qs),
        metrics=_metrics(ver, pp),
    )


def full_report() -> Report:
    """Критичная находка, неясность, нечёткая цитата, OCR, линтер, три вопроса, предупреждения."""
    found = findings()
    ver = _verification(found, REQS, TrafficLight.RED, "критичных нарушений: 1")
    return _report(
        ver,
        questions(),
        REQS,
        status=ReportStatus.PARTIAL,
        input_diagnostics=[
            Diagnostic(
                code="IN-051",
                level=DiagLevel.WARNING,
                message="На стр. 3 нет текстового слоя",
                action="Распознал картинку, цитаты помечены (OCR)",
                hint="Сверьте цитаты со стр. 3 с оригиналом",
                location="стр. 3",
            )
        ],
        diagnostics=[
            Diagnostic(
                code="LLM-021",
                level=DiagLevel.WARNING,
                message="Один из проходов поиска не уложился во время",
                action="Продолжил по двум проходам",
                step="search",
            )
        ],
    )


def clean_report() -> Report:
    """Нарушений и вопросов нет: пустые разделы не пропадают."""
    ver = _verification([], REQS[:3], TrafficLight.GREEN, "нарушений не найдено")
    return _report(ver, [], REQS[:3])


def unread_report() -> Report:
    """Вход не прочитан: паспорт, диагностика входа и итог «не выполнено»."""
    pp = passport(
        document_name="broken.pdf",
        document_kind=DocKind.PDF,
        document_sha256=None,
        document_size_bytes=0,
        duration_s=0.1,
        exit_code=ExitCode.INPUT,
        models={},
        models_actual=[],
        calls_total=0,
        tokens=TokenUsage(),
        cost_usd=None,
        backend_used=None,
    )
    diag = Diagnostic(
        code="IN-011",
        level=DiagLevel.ERROR,
        message="Файл пустой: 0 байт",
        location="весь файл",
        action="Остановил проверку",
        hint="Пришлите исходный файл заново",
    )
    return Report(
        status=ReportStatus.NOT_PERFORMED,
        passport=pp,
        input_diagnostics=[diag],
        conclusion=Conclusion(
            traffic_light=TrafficLight.RED,
            headline="Проверка не выполнена: Файл пустой: 0 байт",
            performed=False,
        ),
    )


CASES = {"full": full_report, "clean": clean_report, "unread": unread_report}


if __name__ == "__main__":
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for name, build in CASES.items():
        (FIXTURES / f"{name}.report.json").write_text(
            build().model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
