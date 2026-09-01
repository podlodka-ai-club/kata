# Раннер прогонов

Раннер поддерживает три режима:

- `memory-off` — coding-agent без памяти;
- `memory-on` — хронологический поток `read → coding task → validated write`;
- `memory-on+evolve` — тот же поток, но после `a3` и до `a4` запускается отдельная curator-session.

Каждая coding task остаётся новой Claude-сессией и одноразовым workspace. В memory modes перед
**каждой** coding/curator-сессией создаётся новый xmemory instance: для первой сессии он
cloud-to-cloud клонируется из read-only C0 template, для следующих — из instance предыдущей
сессии. Поэтому код задач не накапливается, опыт накапливается в цепочке xmemory instances, а
ни одна параллельная экспериментальная ячейка не делит mutable instance.

## Что реализовано

`memory_backend.py` задаёт один backend-контракт. Официальные memory-прогоны требуют xmemory;
файловый backend оставлен только для бесплатного state-machine selftest. В xmemory предметные
facts и Task-relations остаются типизированными. Singleton `MemoryState` хранит canonical JSON
manifest, digest, версии и journal, чтобы новый instance можно было детерминированно собрать из
родительского cloud state. Это не Markdown dump и не контекст агента.

Локальный `_memory/.../lineage.json` содержит только `instance_id`, `parent_instance_id`, версии и
SHA-256 для аудита. В нём нет facts/tasks/journal; удаление локального state не меняет содержимое
xmemory. Runtime read, exact injected content и write-back идут через session instance.

Перед задачей backend выбирает только `active` facts из `task.slices` (и ранжирует их по тексту
задачи до `top_k`). SessionStart инжектит подготовленный ответ, а runner сохраняет точный текст,
fact IDs, chars/estimated tokens, precision/coverage и irrelevant IDs. Полный C0 не передаётся.

Stop-hook возвращает coding-agent на U3 один раз. Агент пишет
`.kata-run/memory_mutations.json`; runner валидирует `create/update/stale/noop`, evidence,
`Task.used_facts/produced_facts`, применяет batch backend’ом и только затем повышает state version.
Следующая новая сессия создаёт child из записанного parent instance и читает изменения уже из
child — это наблюдаемый cloud durability/clone boundary, а не перенос контекста Claude.

Evolution checkpoint не имеет checkout Mealie, будущих задач, solution commits или hidden tests.
Curator видит только cloud `MemoryState`, candidates/gotchas/questions и xmemory schema
suggestions. Он может дедуплицировать, разрешать противоречия по evidence («код прав»), stale/update
существующие facts и принять/отложить schema suggestions. Создавать solution facts ему запрещено.

## Бесплатные проверки

Нужны Python 3.11+ и PyYAML. В проекте удобно запускать через `uv`:

```bash
uv run --python 3.12 --with pyyaml python dataset/runner/sweep.py --memory-selftest
```

Этот fake transport/file selftest не вызывает Claude или xmemory и проверяет isolation mode/seed,
новый cloud child на каждую сессию, отсутствие локального fact-state, chronological write→clone→read,
curator child, task-relevant retrieval, evolution checkpoint, Task provenance, typed xmemory
objects/relations/manifest, `feature_lift` boundaries и analytical eligibility.

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

File backend предназначен только для selftest/debug и не допускается в официальный memory sweep:

```toml
[memory]
backend = "file"
require_xmemory_for_memory_modes = false
snapshot = "dataset/facts/snapshot-c0.md"
retrieval = "task-slices"
top_k = 20
write_back = true
```

Он маркируется `fallback=true`. По умолчанию `require_xmemory_for_memory_modes=true`, поэтому
случайно получить исследовательскую строку без xmemory нельзя.

Для canary/full сначала авторизуйтесь вне репозитория и один раз создайте read-only C0 template.
Команда создаёт schema, типизированные C0 objects и `MemoryState`; она требует xmemory quota,
но не Claude:

```bash
xmemcli auth status
uv run --python 3.12 python dataset/runner/provision_xmemory.py provision \
  --name kata-c0-template --snapshot dataset/facts/snapshot-c0.md
```

Скопируйте полученный `instance_id` в `config.toml`:

```toml
[memory]
backend = "xmemory"
c0_instance_id = "<c0-template-instance-id>"
require_xmemory_for_memory_modes = true
instance_name_prefix = "kata"
```

Runner сам создаёт fresh child перед каждой memory session и проверяет cloud manifest digest.
Canary создаст 5 child instances: 4 coding + curator. Full matrix создаст 39: 36 coding + 3
curator. C0 template общий, но никогда не мутируется. Удаление созданных instances скрипт
намеренно не автоматизирует.

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

Обе команды запускают платные Claude-сессии. Каждый memory task дополнительно делает cloud clone
(parent read, schema read, create, structured seed, verification), relevant read и validated write;
curator создаёт ещё один child и обращается к schema suggestions. Runner не запускает эти команды
из selftest и не запускает автоматически.

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
`attribution.json`. `_memory/<mode>/seedN/lineage.json` содержит только цепочку remote IDs/hashes;
semantic state живёт в xmemory `MemoryState` и типизированных objects/relations. Evolution лежит в
`_evolution/memory-on+evolve/seedN/evolution.json`. Общий `results.csv` содержит child/parent IDs,
clone/read/write/evolve cost отдельно, retrieval, lifecycle mutations, architecture placement и
eligibility.

`files_read` и `time_to_first_relevant_file` записываются как unavailable, если выбранный CLI не
даёт tool trace. Runner не подменяет отсутствие trace выдуманным числом.
