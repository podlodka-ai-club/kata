# Результаты прогонов

Здесь лежат неизменяемые отчёты по завершённым eval-прогонам и компактные CSV для повторного
анализа и графиков. В отчёте обязательно фиксируются модель, effort, версия CLI, commit раннера,
режим памяти, число повторов и ограничения интерпретации.

| Дата | Модель | Повторы | Отчёт | Данные |
| --- | --- | ---: | --- | --- |
| 2026-09-02 | Claude Sonnet 5, medium | precision canary: 1 | [precision-v1 canary — gate FAIL](2026-09-02-sonnet5-xmemory-precision-v1-canary.md) · [attribution](2026-09-02-sonnet5-xmemory-precision-v1-attribution.md) | [CSV](2026-09-02-sonnet5-xmemory-precision-v1-canary.csv) · [hardened gate audit](2026-09-02-sonnet5-xmemory-precision-v1-canary-gate-audit.json) · [проблемы/infra](2026-09-02-sonnet5-xmemory-precision-v1-canary-problems.md) |
| 2026-09-02 | Claude Sonnet 5, medium | full: 2 | [cloud xmemory full matrix](2026-09-02-sonnet5-xmemory-full-r2.md) · [что уже знаем](2026-09-02-sonnet5-xmemory-current-learnings.md) | [CSV](2026-09-02-sonnet5-xmemory-full-r2.csv) · [проблемы прогона](2026-09-02-sonnet5-xmemory-full-r2-problems.md) |
| 2026-09-01 | Claude Sonnet 5, medium | canary: 1 | [cloud xmemory canary a3→a6](2026-09-01-sonnet5-xmemory-canary.md) | [CSV](2026-09-01-sonnet5-xmemory-canary.csv) · [проблемы runner](2026-09-01-sonnet5-xmemory-canary-problems.md) |
| 2026-09-01 | Claude Sonnet 5, medium | 1 | [memory-off vs memory-on](2026-09-01-sonnet5-medium-seed1.md) | [CSV](2026-09-01-sonnet5-medium-seed1.csv) |

Запись 2026-09-01 — сохранённый **naive full-snapshot negative control**: read-only memory,
одинаковый полный dump для всех задач, один repeat. Новые task-relevant read/write/evolve
результаты добавляются отдельными файлами и не переписывают этот baseline.

Один seed — разведочный прогон, а два repeats всё ещё не статистическое доказательство. Full r2
дополнительно оказался слишком разрежен после eligibility gate для причинного сравнения режимов;
детали и рекомендуемый следующий дизайн зафиксированы в его отчёте.

Precision-v1 canary закрыл старую test-interference проблему физической защитой и получил 9/9
technically valid rows, но gate остановил новый full: post-transfer `a6/on+evolve` дал regression,
а learned fact не был использован будущей задачей. Результат публикуется как отрицательный, без
quality rerun.
