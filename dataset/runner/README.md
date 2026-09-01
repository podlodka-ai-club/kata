# Раннер прогонов

Раннер поддерживает три режима:

- `memory-off` — coding-agent без памяти;
- `memory-on` — хронологический поток `read → coding task → validated write`;
- `memory-on+evolve` — тот же поток, но после `a3` и до `a4` запускается отдельная curator-session.

Каждая coding task остаётся новой Claude-сессией и одноразовым workspace. Долговечное состояние
живет отдельно, в уникальном stream `mode/seed`, клонированном из frozen C0. Поэтому код задач не
накапливается, а опыт в памяти — накапливается. Потоки `memory-on` и `memory-on+evolve` одного seed
стартуют из побайтово одинакового C0, но никогда не используют общий state/instance.

## Что реализовано

`memory_backend.py` задаёт один backend-контракт. Primary для demo/evolve — xmemory. Файловый
backend — воспроизводимый bulk/selftest fallback: он сохраняет те же lifecycle/provenance semantics,
Task-relations, journal и версии, но в метриках явно имеет `fallback=true`.

Перед задачей backend выбирает только `active` facts из `task.slices` (и ранжирует их по тексту
задачи до `top_k`). SessionStart инжектит подготовленный ответ, а runner сохраняет точный текст,
fact IDs, chars/estimated tokens, precision/coverage и irrelevant IDs. Полный C0 не передаётся.

Stop-hook возвращает coding-agent на U3 один раз. Агент пишет
`.kata-run/memory_mutations.json`; runner валидирует `create/update/stale/noop`, evidence,
`Task.used_facts/produced_facts`, применяет batch backend’ом и только затем повышает state version.
Следующая новая сессия заново открывает state и читает уже записанные изменения — это durability
boundary, а не перенос контекста Claude.

Evolution checkpoint не имеет checkout Mealie, будущих задач, solution commits или hidden tests.
Curator видит только накопленный audit shadow, candidates/gotchas/questions и xmemory schema
suggestions. Он может дедуплицировать, разрешать противоречия по evidence («код прав»), stale/update
существующие facts и принять/отложить schema suggestions. Создавать solution facts ему запрещено.

## Бесплатные проверки

Нужны Python 3.11+ и PyYAML. В проекте удобно запускать через `uv`:

```bash
uv run --python 3.12 --with pyyaml python dataset/runner/sweep.py --memory-selftest
```

Этот fake/file selftest не вызывает Claude или xmemory и проверяет isolation mode/seed,
chronological write→read, повторное открытие state после «рестарта», task-relevant retrieval,
evolution checkpoint, запрет будущих create из curator-session, Task provenance, typed xmemory
relations, `feature_lift` boundaries и analytical eligibility.

Затем обязательный null/oracle gate всех hidden suites (тоже без модели/xmemory):

```bash
uv run --python 3.12 --with pyyaml python dataset/runner/sweep.py \
  --selftest --skip-setup --config dataset/runner/config.example.toml
```

`null` обязан оставить feature-dependent checks красными; `oracle` накладывает только исходники
эталонного PR (без tests) и обязан пройти. Regression в обеих сторонах должна быть зелёной.

## Конфигурация backend

```bash
cp dataset/runner/config.example.toml dataset/runner/config.toml
```

Для бесплатного/reproducible bulk режима оставьте:

```toml
[memory]
backend = "file"
snapshot = "dataset/facts/snapshot-c0.md"
retrieval = "task-slices"
top_k = 20
write_back = true
```

Это полноценный read/write lifecycle, но не xmemory; отчёты маркируют его fallback.

Для настоящего xmemory demo сначала авторизуйтесь вне репозитория, затем создайте отдельные
инстансы. Команды ниже делают control-plane create и один structured C0 seed; они требуют
xmemory quota, но не Claude:

```bash
xmemcli auth status
python dataset/runner/provision_xmemory.py provision \
  --name kata-memory-on-seed1 --snapshot dataset/facts/snapshot-c0.md
python dataset/runner/provision_xmemory.py provision \
  --name kata-memory-on-evolve-seed1 --snapshot dataset/facts/snapshot-c0.md
```

Скопируйте два `instance_id` в `config.toml`:

```toml
[memory]
backend = "xmemory"

[memory.xmemory_instances]
"memory-on.seed1" = "<instance-id-1>"
"memory-on+evolve.seed1" = "<instance-id-2>"
```

Для full sweep с тремя repeats нужно шесть инстансов: по одному для каждой пары
`(memory-on|memory-on+evolve, seed1|seed2|seed3)`. Runner отвергает пустой неявный id;
не переиспользуйте инстанс между ключами. Удаление инстансов скрипт намеренно не автоматизирует.

## Один run, canary, full matrix

Один `memory-on` run (coding-agent и backend расходы возможны):

```bash
uv run --python 3.12 --with pyyaml python dataset/runner/run.py \
  --config dataset/runner/config.toml \
  --task a3 --mode memory-on --seed 1 \
  --memory-state runs/manual/_memory/memory-on/seed1 \
  --reset-memory --skip-setup --out runs/manual
```

Research canary: `a3 → a6`, один seed, три режима. Для evolving stream checkpoint запускается
сразу после a3, затем новая coding-session a6 читает post-evolution state:

```bash
uv run --python 3.12 --with pyyaml python dataset/runner/sweep.py \
  --config dataset/runner/config.toml \
  --tasks a3 a6 \
  --modes memory-off memory-on memory-on+evolve \
  --seeds 1 --skip-setup \
  --out runs/canary-a3-a6
```

Только после зелёного canary — полная матрица с тремя repeats:

```bash
uv run --python 3.12 --with pyyaml python dataset/runner/sweep.py \
  --config dataset/runner/config.toml \
  --modes memory-off memory-on memory-on+evolve \
  --seeds 3 --skip-setup \
  --out runs/full-read-write-evolve-r3
```

Обе команды запускают платные Claude-сессии; xmemory backend дополнительно расходует read/write и
schema-suggestion quota. Runner не запускает их из selftest и не запускает автоматически.

## Pristine grading и eligibility

Runner снимает agent diff, классифицирует tests как `added`, `modified_existing` или
`deleted_existing`, затем полностью восстанавливает pristine tests базового коммита. Только после
этого запускаются regression и overlay hidden tests. Поэтому ослабление штатных tests не может
улучшить score. Добавленные tests видны в diff, но не становятся grading suite.

`valid_run` — техническая валидность: rc/usage/CLI, memory read+write и оба junit разобраны.
`analytical_eligible` строже: дополнительно требует зелёную regression и отсутствие
изменения/удаления существующих tests. Неeligible строки остаются в CSV для расследования, но не
входят в primary macro comparison.

Primary task score:

```text
feature_lift = (agent_passed - null_passed) / (oracle_passed - null_passed)
```

Нулевой/отрицательный oracle gap даёт `null` + boundary reason, а не деление на ноль. Отрицательный
lift не зажимается: это сигнал вреда/регрессии. `task_success` остаётся дополнительным бинарным
полем. CSV показывает macro-average по задачам, затем median/range по repeats; micro hidden count
сохраняется только как диагностическая метрика.

## Артефакты

Для каждой task: `metrics.json`, pristine `regression.xml`, `hidden.xml`, `diff.patch`,
`memory_read.json`, `memory_write.json`, exact `context_injected.txt`, mutation batch и
`attribution.json`. Состояние stream лежит в `_memory/<mode>/seedN/state.json`; evolution — в
`_evolution/memory-on+evolve/seedN/evolution.json`. Общий `results.csv` содержит coding/read/write/
evolve cost отдельно, retrieval, lifecycle mutations, architecture placement и eligibility.

`files_read` и `time_to_first_relevant_file` записываются как unavailable, если выбранный CLI не
даёт tool trace. Runner не подменяет отсутствие trace выдуманным числом.
