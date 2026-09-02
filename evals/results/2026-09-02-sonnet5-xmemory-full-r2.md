# Cloud-only xmemory full matrix: Sonnet 5 medium, r2

Прогон начат **2026-09-01** и завершён **2026-09-02**. Матрица содержит 6 задач × 3 режима ×
2 repeats: **36/36 coding runs** и **2/2 curator sessions**. Все 38 сессий завершились, все coding
rows технически валидны. Canary сохранён отдельно и в эти числа не включён.

## Главный вывод

Full r2 подтвердил инженерную часть cloud-only протокола, но **не дал валидного причинного
сравнения качества режимов**. После заранее заданного eligibility gate осталось только 8/36 строк:
4 off, 3 on и 1 evolve. Нет ни одной task/seed ячейки, где одновременно eligible все три режима;
единственная eligible пара off/on — a3 seed2, и у обеих сторон feature lift равен 0.000.

Поэтому primary macro ниже описывает разные подмножества задач и не является эффектом памяти:

| Режим | Valid | Eligible | Primary macro median | Range по доступным seeds |
| --- | ---: | ---: | ---: | ---: |
| memory-off | 12/12 | 4/12 | 0.500 | 0.500…0.500 |
| memory-on | 12/12 | 3/12 | 0.300 | 0.200…0.400 |
| memory-on+evolve | 12/12 | 1/12 | 1.000 | 1.000…1.000 |

Значение evolve 1.000 основано только на a5 seed2; сравнивать его с другими режимами нельзя.

## Secondary raw diagnostics

Эта таблица использует все technically valid строки, включая ineligible. Она полезна для поиска
сбоев и дисперсии, но не для primary claim.

| Режим | Hidden seed1 / seed2 | Binary success seed1 / seed2 | Raw macro lift median | Range | Coding cost |
| --- | ---: | ---: | ---: | ---: | ---: |
| memory-off | 97/111 · 97/111 | 2/6 · 2/6 | 0.556 | 0.544…0.567 | $30.849 |
| memory-on | 91/111 · 97/111 | 1/6 · 1/6 | 0.479 | 0.391…0.567 | $29.326 |
| memory-on+evolve | 97/111 · 95/111 | 1/6 · 1/6 | 0.514 | 0.483…0.544 | $32.322 |

На raw scores обычный on сильно просел в seed1, но совпал с off в seed2. Evolve совпал с off по
hidden total в seed1 и оказался на две проверки ниже в seed2. Такая смена картины между двумя
repeats подтверждает высокую вариативность и недостаточность r2.

## Transfer clusters

| Cluster | Off raw / eligible | On raw / eligible | Evolve raw / eligible |
| --- | ---: | ---: | ---: |
| config/API/auth (a2,a4,a5) | 0.667 / 1.000 (n=1) | 0.633 / 0.400 (n=2) | 0.611 / 1.000 (n=1) |
| data/repository/invariants (a1,a3,a6) | 0.444 / 0.333 (n=3) | 0.324 / 0.000 (n=1) | 0.417 / — (n=0) |

Raw cluster averages близки между режимами и не проходят eligibility. Eligible cluster values снова
имеют разные составы, а у evolve/data нет ни одной точки. Transfer benefit не установлен.

## Retrieval, architecture и harmful-memory

- Retrieval coverage во всех 24 memory coding sessions — **1.000** по заранее объявленным
  expected facts. Precision: on median **0.282**, range 0.143…0.500; evolve median **0.282**,
  тот же range. Median context — 1154 tokens on и 1066 evolve (общий range 865…1659).
- Architecture green: 8/12 off, 7/12 on, 7/12 evolve. Средний raw architecture score почти
  одинаков: 0.806 / 0.813 / 0.806.
- `harmful_on_worse_off`: 3 on-строки (a1, a4, a6 seed1) и 2 evolve-строки (a1 и a2 seed2).
- `harmful_regression_after_retrieval`: 6/12 on и 6/12 evolve. Ни одна строка не использовала
  stale fact. Эти флаги показывают корреляцию после retrieval, а не доказывают причинность факта.
- Самый заметный adverse stream — on seed1: hidden 91/111 против off 97/111, четыре regression-red
  задачи и a6 1/9 против off 3/9. В seed2 on вернулся к 97/111, поэтому эффект нестабилен.

## Eligibility и regression

Все 36 rows технически валидны; invalid rows нет. Только 8 аналитически eligible:

- 16 строк исключены только из-за изменения/удаления существующих tests;
- 6 — только из-за regression red;
- 6 — по обеим причинам.

Итого существующие tests затронуты в 22 строках, regression red — в 12. Перед scoring runner
восстанавливал pristine tests, поэтому эти изменения не могли искусственно повысить hidden score,
но заранее заданный analytical gate обязан исключить строки из primary comparison.

## Memory lifecycle и durability

Создано ровно **26 fresh xmemory children**: 24 coding + 2 curator. В каждом stream sessions шли
строго a1→a2→a3→[curator]→a4→a5→a6. Все 26 children имеют delete receipts; после sweep в облаке
остались только три исходных C0 instances, runner tails отсутствуют.

| Stream | Creates | Stale от coding | Gotchas | Final state |
| --- | ---: | ---: | ---: | ---: |
| on seed1 | 13 | 0 | 7 | 6 |
| on seed2 | 14 | 1 | 7 | 6 |
| evolve seed1 | 13 | 1 | 6 | 7 |
| evolve seed2 | 14 | 1 | 6 | 7 |

Curator seed1 сделал 5 noop: подтверждающих данных для promotion не было. Curator seed2 сделал
4 noop, 2 stale и 1 update, объединив три дублирующих sandbox gotchas в один canonical fact.
Schema version осталась 1 в обоих repeats; schema migrations не применялись.

C0 `717c173f-6469-4c4c-b495-e51b3a9cfed1` остался state 0 / schema 1 / 19 facts, digest до и
после: `d45721ecd79dab79237612c444bbac8069cf316cc03d02bb360a2486af79270e`.

## Process и стоимость

Coding sessions использовали **1,135,694 output tokens** и расчётные **$92.497**. Два curator:
11,166 output tokens, $0.754, 11 turns. Итого retained full dataset: **$93.251**. Это оценка Claude
Code по usage, а не подтверждённое списание.

Coding-row xmemory operations: 164 clone provider calls / 836.5 s, 72 read / 231.4 s,
120 write / 620.2 s, 20 parent-retention deletes / 16.1 s. Ещё два parent deletes принадлежат
curator boundaries, четыре — финальному tail cleanup; lineage подтверждает все 26 удалений.
xmemory CLI не предоставляет денежную стоимость этих операций.

Agent wall-clock не используется для межрежимного вывода: ноутбук засыпал во время seed2, поэтому
несколько значений включают suspend-time. Token/cost, test results и cloud digests при этом целы.

## Ограничения и следующий эксперимент

1. Два repeats недостаточны для устойчивой оценки дисперсии.
2. Главный blocker — test-interference gate: 22/36 строк исключены. Нужно либо запретить coding
   agent менять существующие tests на уровне OS/tool policy, либо заранее объявить более точное
   правило, которое допускает только добавленные tests и всё равно scoring делает на pristine tree.
3. Regression-red сконцентрирован в memory modes (12 строк). Перед новым дорогим прогоном нужен
   разбор a1/a2 и on seed1, включая exact retrieved facts и regression failures.
4. Следующий primary run должен требовать paired eligibility на уровне task/seed до агрегации и
   показывать paired deltas; при потере пары ни один из режимов этой ячейки не входит в claim.
5. Canary и full r2 нельзя объединять как третий repeat: canary содержит только a3/a6 и был собран
   после инфраструктурной отладки.

Полные компактные числовые строки без agent prose и локальных путей: [CSV](2026-09-02-sonnet5-xmemory-full-r2.csv).
Инциденты и изменения инфраструктуры: [отдельный журнал](2026-09-02-sonnet5-xmemory-full-r2-problems.md).
