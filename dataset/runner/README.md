# Раннер прогонов

Раннер поддерживает три режима:

- `memory-off` — coding-agent без памяти;
- `memory-on` — хронологический поток `read → coding task → validated write`;
- `memory-on+evolve` — тот же поток с отдельной curator-session на объявленной границе
  (`--curator-after`; full по умолчанию использует `a3 → curator → a4`).

При `retrieval_profile = "precision-v1"` публичные имена строк не меняются, а
`protocol_mode` фиксирует mapping: `memory-on → precision-memory-on` и
`memory-on+evolve → precision-memory-on+evolve`. Старые r2 rows этим не переименовываются.

Каждая coding task остаётся новой Claude-сессией и одноразовым workspace. В memory modes перед
**каждой** coding/curator-сессией создаётся новый xmemory instance: для первой сессии он
cloud-to-cloud клонируется из read-only C0 template, для следующих — из instance предыдущей
сессии. Поэтому код задач не накапливается, опыт накапливается в цепочке xmemory instances, а
ни одна параллельная экспериментальная ячейка не делит mutable instance.

## Что реализовано

`memory_backend.py` задаёт один backend-контракт. Официальные memory-прогоны требуют xmemory;
файловый backend оставлен только для бесплатного state-machine selftest. В xmemory предметные
facts и Task-relations остаются типизированными. `MemoryState` хранит маленький root и canonical
JSON manifest в digest-checked chunks (каждый ниже provider string limit), версии и journal, чтобы
новый instance можно было детерминированно собрать из родительского cloud state. Это не Markdown
dump и не контекст агента.

Локальный `_memory/.../lineage.json` содержит только `instance_id`, `parent_instance_id`, версии,
SHA-256 и delete receipts для аудита. В нём нет facts/tasks/journal. Runtime read, exact injected
content и write-back идут через session instance. При ephemeral retention предыдущий child удаляется
только после успешного clone + digest verification, а tail — после завершения целого stream; C0 не
мутируется и не удаляется.

Перед задачей backend выбирает только `active` facts. Precision-v1 даёт score 1.0 только заранее
объявленным C0 architecture facts, допускает не более одного прошедшего проверки learned fact из
того же transfer cluster, отсекает tooling/sandbox/gotcha context и применяет threshold/top-k до
provider read. Ответ provider не может добавить ID вне reranked candidate set. SessionStart
инжектит подготовленный ответ, а runner сохраняет exact text/content digest, ID/origin,
selection/rejection reasons, chars/estimated tokens, precision/coverage. Полный C0 не передаётся.

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

Отдельный обязательный pre-Claude test-protection preflight (macOS host, потому что он проверяет
Seatbelt и immutable flags):

```bash
uv run --python 3.12 --with pyyaml python dataset/runner/sweep.py \
  --test-protection-selftest --out runs/precision-v1-canary
```

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
delete_parent_after_clone = true
delete_stream_tail = true
retrieval_profile = "precision-v1"
top_k = 5
relevance_threshold = 0.75
learned_top_k = 1
```

Runner сам создаёт fresh child перед каждой memory session и проверяет cloud manifest digest.
Precision canary `a1,a4,a6` создаёт 7 child instances: 6 coding + curator. Full matrix с двумя
repeats создаёт 26: 24 coding + 2 curator. C0 template общий и никогда не мутируется. Чтобы укладываться в ограничение
cloud-retention, runner удаляет только собственный verified parent и финальный tail; глобального
«удалить самый старый instance организации» нет.

## Один run, canary, full matrix

Один `memory-on` run (coding-agent и backend расходы возможны):

```bash
uv run --python 3.12 --with pyyaml python dataset/runner/run.py \
  --config dataset/runner/config.toml \
  --task a3 --mode memory-on --seed 1 \
  --memory-state runs/manual/_memory/memory-on/seed1 \
  --reset-memory --skip-setup --out runs/manual
```

Precision canary: `a1 → a4 → a6`, один seed, три режима. Для evolving stream checkpoint
запускается после a4, затем новая coding-session a6 читает post-evolution state. `--canary-gate`
фиксирует exact форму canary, paired eligibility, regression, attribution, retrieval,
test-protection, retention receipts и C0 digest:

Сначала обязательный cloud-only durability gate проверяет real read/write/clone/read, два scoped
delete receipt и неизменность C0. Он не запускает Claude, но обращается к xmemory provider:

```bash
uv run --python 3.12 --with pyyaml python dataset/runner/cloud_preflight.py \
  --config dataset/runner/config.toml \
  --out runs/precision-v1-cloud-preflight
```

```bash
uv run --python 3.12 --with pyyaml python dataset/runner/sweep.py \
  --config dataset/runner/config.toml \
  --tasks a1 a4 a6 \
  --modes memory-off memory-on memory-on+evolve \
  --seeds 1 --skip-setup --fail-fast \
  --curator-after a4 --canary-gate \
  --out runs/precision-v1-canary
```

Если sweep оборвался на явной transient-ошибке провайдера, `--resume` сохраняет уже technically
valid memory-off cells; ineligible строки не перезапускаются ради более удобного результата, а
остаются для расследования и исключаются только из primary comparison. Полностью завершённый
valid memory stream можно сохранить целиком. Частичный memory stream не склеивается и не
перезаписывается: resume fail-closed требует сохранить lineage/receipts и начать весь эксперимент
в новом output directory. Runner делает один автоматический retry лишь
для распознанной Claude API / network ошибки при пустом source diff и суммирует стоимость обеих
попыток.

Только после зелёного canary — полная матрица с двумя repeats:

```bash
uv run --python 3.12 --with pyyaml python dataset/runner/sweep.py \
  --test-protection-selftest --out runs/full-precision-v1

uv run --python 3.12 --with pyyaml python dataset/runner/sweep.py \
  --config dataset/runner/config.toml \
  --modes memory-off memory-on memory-on+evolve \
  --seeds 2 --skip-setup --curator-after a3 \
  --protection-preflight-receipt runs/full-precision-v1/_preflight/test_protection.json \
  --out runs/full-precision-v1
```

Первая команда — бесплатный physical protection preflight; Claude-сессии запускает вторая.
Каждый memory task дополнительно делает cloud clone
(parent read, schema read, create, structured seed, verification), relevant read и validated write;
curator создаёт ещё один child и обращается к schema suggestions. Runner не запускает эти команды
из selftest и не запускает автоматически.

## Pristine grading и eligibility

До coding runner строит hash/mode/git-index manifest существующих `tests/**`, физически защищает
всё дерево immutable flags + Claude sandbox + fail-closed Edit/Write guard. Agent может добавлять
tests только в sibling `agent_tests/`. Сразу после coding и **до memory write/scoring** runner
доказывает held protection и исходное hash/kind/mode/index equality; failure закрывает строку без
regression/hidden. Затем grading всё равно получает pristine base tests как независимую защиту.
Добавленные `agent_tests` видны в diff, но не становятся grading suite.

`valid_run` — техническая валидность: rc/usage/CLI, memory read+write и оба junit разобраны.
`analytical_eligible` строже: дополнительно требует зелёную regression и отсутствие
изменения/удаления существующих tests. Неeligible строки остаются в CSV для расследования, но не
входят в primary macro comparison.

Paired gate применяется на уровне `task/seed`: ячейка входит в primary только если ровно одна
строка каждого объявленного режима одновременно valid и analytical eligible. При выпадении одного
режима все строки ячейки остаются только в raw diagnostics. Валидный плохой quality outcome не
перезапускается.

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
`attribution.json`, base manifest и pre-scoring protection proof. `_memory/<mode>/seedN/lineage.json`
содержит только цепочку remote IDs/hashes;
semantic state живёт в xmemory `MemoryState` и типизированных objects/relations. Evolution лежит в
`_evolution/memory-on+evolve/seedN/evolution.json`. Общий `results.csv` содержит child/parent IDs,
clone/read/write/evolve cost отдельно, retrieval, lifecycle mutations, architecture placement и
eligibility.

`files_read` и `time_to_first_relevant_file` записываются как unavailable, если выбранный CLI не
даёт tool trace. Runner не подменяет отсутствие trace выдуманным числом.
