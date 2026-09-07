# Каркас прогонов и метрики

Исполняемый harness находится в [`dataset/runner/`](../dataset/runner/). Он сравнивает три режима:
`memory-off`, последовательный read/write `memory-on` и `memory-on+evolve` с отдельной
curator-session после a3. Code workspace каждой задачи одноразовый; memory state хронологический,
durable и изолирован по mode/seed.

## Оси измерения

| Ось | Поля |
| --- | --- |
| Primary quality | `feature_lift`, feature-dependent passed/total, macro-average по задачам |
| Secondary quality | micro hidden passed/total, binary `task_success` |
| Validity | `valid_run` отдельно от `analytical_eligible` и причин исключения |
| Pristine tests | added tests отдельно от modified/deleted existing; regression/hidden после restore base tests |
| Retrieval | exact fact IDs/content, count/chars/tokens, expected precision/coverage, irrelevant facts |
| Memory | create/update/stale/noop, gotchas, used/produced facts, state/schema versions |
| Architecture | predeclared required layers и forbidden shortcuts, отдельно от hidden behavior |
| Process/cost | coding/read/write/evolve wall и usage отдельно, turns, files changed |
| Attribution/harm | fact→read→Task link→diff/check; on хуже off, stale used, post-retrieval regression |

Primary score на задаче:

```text
feature_lift = (agent_passed - null_passed) / (oracle_passed - null_passed)
```

При неположительном gap значение аналитически не определено. Отрицательные значения сохраняются
как harmful signal. В summary сначала считается macro-average по eligible задачам каждого seed,
затем median/range по трём repeats; один seed не интерпретируется каузально.

## Research gate

1. Бесплатно: fake-backend state machine и полный null/oracle selftest.
2. Canary: только `a3→a6`, один seed, три режима.
3. Только при зелёном canary: `a1→a2→a3→evolve→a4→a5→a6`, три repeats.

Точные команды, backend setup и список артефактов — в
[`dataset/runner/README.md`](../dataset/runner/README.md).
