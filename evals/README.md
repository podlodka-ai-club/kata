# Eval protocol: хронологическая память на фичах из будущего

Мы реплеим реальные Mealie PR после C0. Формулировка приходит из issue/описания, код каждой задачи
стоит на родителе эталонного PR, а solution commit и hidden tests недоступны coding-agent. Память
изначально содержит только факты C0.

Архивный Sonnet 5 seed1 сохранён как **naive full-snapshot negative control**: один полный
read-only dump во все задачи. Он проверял ранний контур, но не real write→read и не task-relevant
retrieval. Новый primary protocol не пересчитывает и не переписывает этот baseline.

## Три экспериментальных stream

- `memory-off`: новая coding-session на каждую задачу, памяти нет;
- `memory-on`: `a1→a2→a3→a4→a5→a6`, перед каждой задачей scoped active read, после — validated
  mutation batch и Task links; запись видна новой сессии следующей задачи;
- `memory-on+evolve`: отдельный clone того же C0 и тот же поток, но между a3/a4 curator-session.

Code workspaces независимы и всегда создаются на `task.base_commit`; agent diffs не перетекают.
Memory state намеренно перетекает только внутри одного mode/seed. У каждой пары mode/seed свой
file state или xmemory instance. Это исключает загрязнение между режимами/repeats.

Curator не видит репозиторий, a4–a6, solution commits или hidden tests. Он ревьюит только уже
накопленные candidates/gotchas/questions и xmemory schema suggestions, следует правилу «код
прав», дедуплицирует, stale/supersede и может применить явно подтверждённую schema migration.

## Scoring и валидность

Primary score — feature-dependent lift относительно обязательных null/oracle selftests:

```text
feature_lift = (agent_passed - null_passed) / (oracle_passed - null_passed)
```

Неположительный oracle gap делает точку неeligible с boundary reason; отрицательный lift не
зажимается. Micro hidden ratio и binary `task_success` остаются secondary diagnostics.

`valid_run` означает техническую валидность: agent rc/usage/CLI, memory read+write и junit
разобраны. `analytical_eligible` дополнительно требует зелёную regression и запрещает
изменение/удаление существующих tests. Добавленные tests считаются отдельно и видны в diff.

Перед regression runner восстанавливает весь pristine `tests/` из base commit, затем накладывает
hidden tests эталонного PR. Поэтому ослабление теста агентом не может повысить score. Красная
regression и test interference остаются в CSV, но исключаются из primary comparison.

Summary считается macro по задачам внутри seed, затем показывает median/range по трём repeats.
Один seed — наблюдение, не каузальный вывод.

## Retrieval, architecture и attribution

Заранее объявленные `task.slices` и `expected_facts` не попадают в prompt. File fallback
детерминированно фильтрует sections по slices и text score; xmemory получает scoped/top-k query.
Логируются exact injected ID/content, chars/tokens, precision/coverage и irrelevant facts.

Для class A в `tasks.yaml` заранее объявлены deterministic `required_path_groups` и forbidden
shortcuts: migration, expected layer, repository/controller convention, tenant scope. Они не
содержат solution text и не доступны agent. Class B допускает отдельного blinded judge, которому
показывают только обезличенный diff и reference после coding run. Tests/solution leakage в memory
запрещены.

`attribution.json` связывает fact/lesson → exact read → Task.used/produced links → diff paths →
hidden/architecture outcome. Harmful indicators выводятся из парных результатов: on ниже off,
stale fact в used_facts или regression после retrieval. Где CLI не даёт tool trace,
`files_read/time_to_first_relevant_file` остаются `null`, а не оцениваются на глаз.

## Transfer clusters и план запуска

Две аналитические дорожки:

- config/API/auth: `a2→a4→a5`;
- data/repository/invariants: `a1→a3→a6`.

Сначала бесплатные fake/null/oracle gates. Затем один платный canary `a3→evolve→a6` во всех трёх
режимах. Только после зелёной regression, write→read и durability trace запускается full matrix с
двумя repeats. Точные команды — в [`dataset/runner/README.md`](../dataset/runner/README.md).

Зафиксированные результаты складываются в [`results/`](results/README.md). Исходная архивная
точка: [Sonnet 5 medium, seed 1, 2026-09-01](results/2026-09-01-sonnet5-medium-seed1.md).
