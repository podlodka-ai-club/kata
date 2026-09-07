# Cloud-only xmemory canary: Sonnet 5 medium, a3→a6

Дата: **2026-09-01**. Это платный canary нового chronological read→write→clone→read
протокола перед полной матрицей. Он не заменяет архивный negative control из PR #8.

## Итог

Canary завершил все **6/6 coding runs** и отдельный curator checkpoint. Все строки технически
валидны, regression suites зелёные, C0 остался неизменным, а пять дочерних xmemory-инстансов
удалены только после проверки следующего clone/read либо завершения stream. Два a6-run
аналитически неeligible из-за изменения существующих тестов; они сохранены, но исключены из
primary comparison.

| Режим | Coding runs | Eligible | Macro feature lift¹ | Binary success | Coding cost |
| --- | ---: | ---: | ---: | ---: | ---: |
| memory-off | 2/2 | 1/2 | 0.000 | 0/2 | $10.125 |
| memory-on | 2/2 | 2/2 | 0.167 | 0/2 | $6.776 |
| memory-on+evolve | 2/2 | 1/2 | 0.000 | 0/2 | $12.256 |

¹ Macro использует только eligible-строки, поэтому значения между режимами имеют разный состав
задач и **не являются честной парной дельтой**. На единственной полностью eligible паре a3 lift
равен 0.000 во всех режимах. В raw-результатах a6 lift равен 0.333 во всех трёх режимах — видимого
эффекта памяти на качество в этом seed нет.

## Результаты задач

| Task | Режим | Hidden | Feature lift | Architecture | Eligible | Retrieval precision / coverage |
| --- | --- | ---: | ---: | ---: | --- | ---: |
| a3 | off | 23/27 | 0.000 | 0.000 | да | — |
| a3 | on | 23/27 | 0.000 | 0.667 | да | 0.143 / 1.000 |
| a3 | evolve | 23/27 | 0.000 | 0.333 | да | 0.143 / 1.000 |
| a6 | off | 3/9 | 0.333 | 0.800 | нет: modified tests | — |
| a6 | on | 3/9 | 0.333 | 0.800 | да | 0.444 / 1.000 |
| a6 | evolve | 3/9 | 0.333 | 0.800 | нет: modified tests | 0.364 / 1.000 |

Ни одна задача не достигла binary success или полностью зелёной architecture-проверки.
Harmful-memory flags во всех строках false: on не ниже соответствующего off по lift, stale facts
не использованы, regression после retrieval не возникла. Это диагностический факт, а не
доказательство безопасности памяти на большем наборе.

## Memory lifecycle и durability

- `memory-on`: state `0→1` после a3 и `1→2` после a6; создано 5 фактов, включая 2 gotchas.
- `memory-on+evolve`: state `0→1→2(curator)→3`; coding создал 5 фактов, 2 gotchas и один noop.
- Curator продвинул `ac-0005`, `ac-0006`, `gt-0001`, оставил `ac-0004` без изменений и не применял
  schema migration; schema suggestion сохранён только как рекомендация.
- Remote operations: 26 clone provider calls / 116.0 s, 12 read / 34.3 s, 20 write / 108.8 s.
  Финальное удаление пяти детей заняло 4.2 s; в coding CSV непосредственно привязаны два
  parent-delete receipt, остальные receipts лежат в lineage/stream cleanup.
- Immutable C0: `717c173f-6469-4c4c-b495-e51b3a9cfed1`, digest до/после
  `d45721ecd79dab79237612c444bbac8069cf316cc03d02bb360a2486af79270e`.

## Стоимость

Retained coding runs: **$29.157**. Curator: **$0.305**, итого canary dataset **$29.461**.
Из-за отладки инфраструктуры было выполнено 22 coding-attempt artifacts и две curator attempts;
полная исследовательская стоимость, включая отброшенные технические попытки, — **$84.783**.
Dollar cost — оценка Claude Code по usage, не подтверждение фактического списания.

## Ограничения интерпретации

Canary собран resume-механизмом после инфраструктурных ошибок, но каждый сохранённый memory-stream
непрерывен и хронологичен. Валидные ineligible строки не перезапускались для выбора более удобного
результата. Один seed и две задачи не дают каузального вывода; полный matrix на двух repeats нужен
для median/range и transfer-cluster анализа. Детали сбоев и исправлений вынесены в
[отдельный журнал](2026-09-01-sonnet5-xmemory-canary-problems.md), числовые строки — в
[CSV](2026-09-01-sonnet5-xmemory-canary.csv).
