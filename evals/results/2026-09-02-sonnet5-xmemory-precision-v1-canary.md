# Precision xmemory canary v1: Sonnet 5 medium

Работа началась **2026-09-02** с clean HEAD `8eb2bb2`; canary использовал новый precision runner,
который публикуется вместе с этим отчётом и последующим fail-closed hardening. Новый experiment id:
`sonnet5-xmemory-precision-v1-2026-09-02`. Старые canary/full r2 rows и baseline PR #8 не
переиспользовались и не изменялись.

Матрица: `a1 → a4 → a6`, три режима, один repeat — **9/9 technically valid coding rows** и
**1/1 curator session**. Режимы в CSV сохраняют публичные имена `memory-off`, `memory-on`,
`memory-on+evolve`; внутреннее описание двух memory modes — `precision-memory-on` и
`precision-memory-on+evolve`.

## Решение gate

**FAIL; full matrix не запущена.** На post-transfer `a6/seed1` режим
`memory-on+evolve` получил regression red и 1/9 hidden против 3/9 в off/on. Это валидный
quality outcome, а не технический сбой, поэтому строка не перезапускалась. Вся тройка `a6`
исключена paired gate из primary comparison.

| Gate | Результат |
| --- | --- |
| Technically valid rows | 9/9 |
| Полностью paired eligible task/seed | 2/3 (`a1`, `a4`) |
| Base tests pristine до scoring | 9/9 |
| Regression green | 8/9 |
| Retrieval precision | median 1.000, range 0.800…1.000 |
| Retrieval coverage | 1.000 во всех 6 memory rows |
| Hidden/future leakage | 0 нарушений |
| xmemory children / actual delete receipts | 7/7 |
| Hardened chronological retention proof | нет: curator label/retention artifact неполны |
| C0 digest | неизменен |
| Claimed-use attribution полностью трассируется | нет: 2 trace holes |

Последние два отрицательных пункта обнаружены post-run аудитом. Опубликованный runner artifact
`canary_gate.json` сохраняет исходную pre-hardening оценку; отдельная воспроизводимая
[hardened gate audit](2026-09-02-sonnet5-xmemory-precision-v1-canary-gate-audit.json) фиксирует
повторный расчёт и не подменяет исходный artifact. Старый `attribution.complete`
означал наличие артефакта, но не требовал decision/path для каждого claimed-used fact. Кроме того,
curator реально стоял после a4 и все children удалены, но lineage сохранил старый label
`evolve-after-a3`, а `evolution.json` не продублировал parent-retention receipt. Hardened gate
теперь fail-closed на обоих пробелах. Это не меняет итог FAIL и не переписывает model rows.

## Paired primary и raw diagnostics

Primary включает только ячейки, где все три режима одновременно valid и analytical eligible.

| Task | Off | Precision on | Precision on+evolve | Paired delta on−off | Paired delta evolve−off |
| --- | ---: | ---: | ---: | ---: | ---: |
| a1 | 1.000 (6/6 feature) | 1.000 | 1.000 | 0.000 | 0.000 |
| a4 | 0.600 (3/5) | 0.600 | 0.600 | 0.000 | 0.000 |

Primary macro для каждого режима равен **0.800**. Один repeat не даёт range между seeds;
наблюдаемый singleton range — 0.800…0.800. По clusters primary содержит только `a1` для
data/repository/invariants и `a4` для config/API/auth, поэтому в обоих clusters дельта равна нулю.

`a6` остаётся только raw diagnostic:

| Task | Off | Precision on | Precision on+evolve | Причина исключения |
| --- | ---: | ---: | ---: | --- |
| a6 | 0.333 (3/9) | 0.333 (3/9) | 0.111 (1/9) | evolve regression red: 30 failed, 10 errors |

Invalid rows: **0**. Analytical ineligible rows: **1** (`a6/on+evolve`). Paired ineligible rows:
**3** — весь `a6/seed1`, потому что один режим ячейки не прошёл gate.

## Проверка суженной гипотезы

Canary не показал, что небольшой высокоточный набор подтверждённых архитектурных фактов улучшает
будущую задачу:

- `a1` уже насыщен: все режимы 1.000;
- `a4`: обе memory-версии совпали с off на 0.600;
- в `a6/on` был выбран один learned fact `do-0007`, но он описывает punctuation normalization,
  не был заявлен как использованный и не дал lift относительно off;
- в `a6/on+evolve` learned facts не были выбраны вообще: строгий reranker оставил только C0.

Поэтому **подтверждённого learned-transfer воздействия на будущую задачу в canary нет**. Это
отдельный design outcome: precision вырос, но evolve-stream не доставил релевантный learned fact
через границу `a4 → a6`. Ухудшение evolve нельзя причинно приписать curator или learned memory.

## Retrieval и exact attribution

| Task/mode | Injected facts | Precision / coverage |
| --- | --- | ---: |
| a1/on, evolve | `do-0005`, `do-0006` (C0) | 1.000 / 1.000 |
| a4/on, evolve | `ac-0001`, `ac-0002`, `iv-0001` (C0) | 1.000 / 1.000 |
| a6/on | `ac-0002`, `do-0001..3` (C0), `do-0007` (learned) | 0.800 / 1.000 |
| a6/evolve | `ac-0002`, `do-0001..3` (C0) | 1.000 / 1.000 |

Median context — **394 estimated tokens**, range 308…919. Для сравнения full r2 имел median
precision 0.282; новый median 1.000 materially выше. Exact content, origin, selection/rejection
reason и content SHA-256 сохранены в row artifacts.

Adverse `a6/evolve` имеет полный trace только для `ac-0002 → controller_categories.py /
controller_tags.py` и `do-0003 → repository_factory.py`. Реальный дефект находится в других
paths: в `category.py` и `tag.py` добавлены `column_property` subqueries без явной correlation,
что вызвало SQLAlchemy auto-correlation errors по regression suite. Ни один claimed-used fact не
указывает на эти model paths. Это trace association с diff defect, **не доказательство причинности
памяти**. Полный offline разбор: [attribution report](2026-09-02-sonnet5-xmemory-precision-v1-attribution.md).

## Process cost

Coding rows: **327,301 output tokens**, расчётные **$30.441**. Curator: 2,726 output tokens,
$0.280. Итого canary: **330,027 output tokens, $30.721**. Это usage estimate Claude Code, а не
подтверждённое списание.

| Режим | Coding output tokens | Coding cost |
| --- | ---: | ---: |
| memory-off | 93,201 | $9.012 |
| precision memory-on | 132,114 | $12.197 |
| precision memory-on+evolve | 101,986 | $9.231 |

С curator evolve total равен 104,712 output tokens и $9.511. Наблюдаемого process-cost reduction
нет: on дороже off на 41.8% по output tokens и 35.4% по coding cost; evolve с curator дороже off
на 12.3% и 5.5% соответственно. Один repeat и вариативность поведения не позволяют считать эти
разницы причинным cost effect, но условие «без роста process cost» canary не поддерживает.

xmemory coding operations: 40 clone provider calls / 179.2 s, 18 read calls / 72.7 s,
30 write calls / 152.0 s. Curator занял 78.1 s, из них backend 21.4 s. Денежную стоимость
xmemory CLI не сообщает.

## Lifecycle, retention и граница curator

Canary использовал границу `a1 → a4 → curator → a6` в evolve-stream. Она выбрана так, чтобы
`a6` была единственной чистой post-transfer задачей canary; curator не видел checkout, future task,
solution commit или hidden tests. Он оставил три непроверенных a4 candidates через `noop`, не
изменил schema и C0.

Создано ровно **7 runner-owned xmemory children**: 6 coding + 1 curator. Для всех 7 есть scoped
delete receipts с `ok=true` и exact instance ID; глобального удаления «самого старого» не было.
У четырёх из пяти parent transitions есть отдельное доказательство `after_verified_clone`;
legacy curator transition подтверждён lineage/delete receipt, но не сохранил этот дополнительный
receipt, поэтому строгий retention proof считается неполным. C0 state/schema/facts не
мутировались; digest до и после:
`d45721ecd79dab79237612c444bbac8069cf316cc03d02bb360a2486af79270e`.

Компактные строки: [CSV](2026-09-02-sonnet5-xmemory-precision-v1-canary.csv). Поля
`attribution_traced_used_count` и `promoted_transfer_facts_count` — счётчики, а единственная
curator-сессия учитывается только на первой post-boundary строке `a6/evolve`, чтобы CSV можно было
суммировать без двойного учёта.
Технические preflight и проблемы: [infra report](2026-09-02-sonnet5-xmemory-precision-v1-canary-problems.md).
