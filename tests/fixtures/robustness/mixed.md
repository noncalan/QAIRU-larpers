# Сервис «Курьерский терминал»: Requirements / Требования

Версия 0.4. Документ ведут два подразделения, поэтому часть требований написана по-русски, а часть по-английски (This document is maintained by two teams, so some requirements are written in Russian and some in English).

## 1. Функциональные требования / Functional requirements

| ID | Требование / Requirement | Приоритет |
|---|---|---|
| FR-01 | Курьер должен иметь возможность принять заказ из списка доступных заказов. | Высокий |
| FR-02 | The system shall hide an order from other couriers as soon as one courier accepts it. | Высокий |
| FR-03 | Система должна рассчитывать оплату курьера по тарифу, который утверждается позже. | Высокий |
| FR-04 | The courier shall confirm delivery by entering the one-time code that the customer received by SMS. | Высокий |
| FR-05 | Диспетчер может переназначить заказ другому курьеру, если курьер не отвечает долгое время. | Средний |

## 2. Нефункциональные требования / Non-functional requirements

NFR-01. Список заказов должен обновляться в реальном времени.

NFR-02. The order list shall refresh at least once every 60 seconds.

NFR-03. Мобильное приложение должно быть удобным и поддерживать Android and iOS.

NFR-04. All personal data shall be stored in accordance with applicable law.

## 3. Критерии приёмки / Acceptance criteria

AC-01. Курьер принял заказ, заказ пропал из списка у остальных курьеров.

AC-02. Code entered by the courier does not match the customer's code: the delivery is not confirmed.
