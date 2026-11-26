"""Метрики разметки precision (#49): precision строгая и мягкая, согласие, каппа, контрольные находки.

- precision строгая: настоящие / размеченные; мягкая: (настоящие + спорно) / размеченные;
- по согласию обоих: строгая это «оба сказали настоящий», мягкая это «оба сказали настоящий или
  спорно»; знаменатель это находки, которые разметили оба;
- согласие: доля одинаковых меток и каппа Коэна по трём меткам и по двум (настоящий или нет);
- контрольные находки: доля размеченных как ложные (поймано) и как не настоящие (ложные или спорно).

Контрольные находки в precision не входят: они только проверяют разметчиков.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from evals.precision_items import LabelItem, Origin
from evals.precision_labeler import Label, Labeler

Labels = Mapping[str, Label]
"""Метки одного разметчика по uid находки."""


class Precision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    total: int = Field(description="Находок в срезе")
    labeled: int = Field(description="Из них размечено")
    real: int = 0
    disputed: int = 0
    false: int = 0

    @property
    def strict(self) -> float | None:
        return self.real / self.labeled if self.labeled else None

    @property
    def soft(self) -> float | None:
        return (self.real + self.disputed) / self.labeled if self.labeled else None


def precision(uids: Sequence[str], labels: Labels) -> Precision:
    counts = Counter(labels[u] for u in uids if u in labels)
    return Precision(
        total=len(uids),
        labeled=sum(counts.values()),
        real=counts[Label.REAL],
        disputed=counts[Label.DISPUTED],
        false=counts[Label.FALSE],
    )


def consensus(a: Labels, b: Labels) -> dict[str, Label]:
    """Метка по согласию: строгая precision считает «оба настоящий», мягкая «оба не ложный».

    Возвращает для находок, размеченных обоими: REAL, если оба REAL; FALSE, если хотя бы один FALSE;
    иначе DISPUTED (оба не ложные, но кто-то сказал «спорно»).
    """
    out: dict[str, Label] = {}
    for uid in a.keys() & b.keys():
        pair = {a[uid], b[uid]}
        if pair == {Label.REAL}:
            out[uid] = Label.REAL
        elif Label.FALSE in pair:
            out[uid] = Label.FALSE
        else:
            out[uid] = Label.DISPUTED
    return out


def kappa(pairs: Sequence[tuple[str, str]]) -> float | None:
    """Каппа Коэна по парам меток. ``None``, если пар нет или ожидаемое согласие равно 1."""
    n = len(pairs)
    if not n:
        return None
    observed = sum(x == y for x, y in pairs) / n
    left = Counter(x for x, _ in pairs)
    right = Counter(y for _, y in pairs)
    expected = sum(left[k] * right[k] for k in left.keys() | right.keys()) / (n * n)
    if expected >= 1:
        return None
    return (observed - expected) / (1 - expected)


class Agreement(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    both: int = Field(description="Находок, размеченных обоими")
    same: int = 0
    kappa3: float | None = Field(default=None, description="Каппа Коэна по трём меткам")
    kappa2: float | None = Field(default=None, description="Каппа Коэна: настоящий или нет")

    @property
    def share(self) -> float | None:
        return self.same / self.both if self.both else None


def _binary(label: Label) -> str:
    return "real" if label is Label.REAL else "not_real"


def agreement(uids: Iterable[str], a: Labels, b: Labels) -> Agreement:
    pairs = [(a[u], b[u]) for u in uids if u in a and u in b]
    return Agreement(
        both=len(pairs),
        same=sum(x == y for x, y in pairs),
        kappa3=kappa([(x.value, y.value) for x, y in pairs]),
        kappa2=kappa([(_binary(x), _binary(y)) for x, y in pairs]),
    )


class ControlScore(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    total: int
    labeled: int
    caught: int = Field(description="Размечены как ложные")
    not_real: int = Field(description="Размечены как ложные или спорно")

    @property
    def caught_share(self) -> float | None:
        return self.caught / self.labeled if self.labeled else None

    @property
    def not_real_share(self) -> float | None:
        return self.not_real / self.labeled if self.labeled else None


def control_score(uids: Sequence[str], labels: Labels) -> ControlScore:
    marks = [labels[u] for u in uids if u in labels]
    return ControlScore(
        total=len(uids),
        labeled=len(marks),
        caught=marks.count(Label.FALSE),
        not_real=sum(m is not Label.REAL for m in marks),
    )


class Slice(BaseModel):
    """Срез находок: precision у каждого разметчика и по согласию."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    by_labeler: dict[Labeler, Precision]
    consensus: Precision
    agreement: Agreement


def make_slice(name: str, uids: Sequence[str], labels: Mapping[Labeler, Labels]) -> Slice:
    by = {lab: precision(uids, marks) for lab, marks in labels.items()}
    pair = list(labels.values())
    if len(pair) == 2:
        agreed = consensus(pair[0], pair[1])
        return Slice(
            name=name, by_labeler=by, consensus=precision(uids, agreed), agreement=agreement(uids, *pair)
        )
    return Slice(
        name=name, by_labeler=by, consensus=Precision(total=len(uids), labeled=0), agreement=Agreement(both=0)
    )


def slices_by(
    items: Sequence[LabelItem],
    key: Callable[[LabelItem], str | None],
    labels: Mapping[Labeler, Labels],
    order: Callable[[str], tuple[int, str]] | None = None,
) -> list[Slice]:
    """Срезы по значению ``key`` в порядке ``order`` (иначе по первому появлению); без значения не идут."""
    groups: dict[str, list[str]] = {}
    for item in items:
        value = key(item)
        if value is not None:
            groups.setdefault(value, []).append(item.uid)
    names = sorted(groups, key=order) if order else list(groups)
    return [make_slice(name, groups[name], labels) for name in names]


def real_items(items: Sequence[LabelItem]) -> list[LabelItem]:
    return [i for i in items if i.origin is not Origin.CONTROL]
