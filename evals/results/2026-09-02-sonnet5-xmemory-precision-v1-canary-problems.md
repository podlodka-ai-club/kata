# Precision-v1 canary: проблемы и инфраструктура

## До платного запуска

Работа началась с clean worktree на `8eb2bb264d9a3f868e95e56165b0451f142ce7e3` в ветке
`report/sonnet5-memory-eval`. Существующие canary/full r2 artifacts и baseline PR #8 не менялись.

Бесплатный gate перед Claude:

- compile и unit/selftests — green;
- null/oracle всех 6 задач — 12/12 framework rows: null сохраняет feature gap, oracle 6/6 green,
  regression в обеих сторонах green;
- adversarial test-protection preflight — green, 233 manifest entries;
- реальный xmemory read→write→clone→read durability — green: sentinel прочитан из child clone,
  2/2 children удалены scoped, C0 digest не изменён.

После усиления claimed-use validator тот же real durability preflight повторён в новом
gitignored каталоге: снова green, 2/2 exact scoped receipts, durable sentinel и тот же C0 digest.

## Физическая защита tests

Для каждой coding session runner строит manifest всех существующих `tests/**`: repo-relative path,
lstat kind, POSIX mode, SHA-256, git index mode/blob и canonical manifest digest. Новые тесты
разрешены только в sibling `agent_tests/`.

Во время coding действуют три слоя:

1. macOS immutable flags и readonly tree для base tests;
2. Claude sandbox denyWrite для `tests`, runtime guards и `.git`;
3. fail-closed PreToolUse guard для Edit/Write с нормализацией absolute/relative/`..`/symlink paths.

После Claude, **до memory apply и scoring**, проверяются held immutable flags и protected
hash/kind/mode, затем после unlock — исходные hash/kind/POSIX mode и git index. При ошибке runner
не запускает regression/hidden и не пишет memory batch. Все 9 canary rows прошли proof; modified
existing tests = 0, deleted existing tests = 0. Добавленные `agent_tests` остаются только в diff и
не входят в grading suite.

Adversarial preflight проверил write/truncate/chmod/chflags-clear/unlink/file rename/tree rename,
создание под `tests`, traversal и symlink aliases, а также arbitrary Python под Seatbelt. Все
запрещённые операции заблокированы; создание/удаление `agent_tests` разрешено. Claude CLI был
`2.1.212`, поэтому собственный Edit|Write hook сохранён как обязательная защита.

## Canary outcome

Сессии шли без параллелизма и без retry:

```text
memory-off:       a1 → a4 → a6
memory-on:        a1 → a4 → a6
memory-on+evolve: a1 → a4 → curator → a6
```

Все 9 coding rows technically valid. Один реальный adverse row — `a6/on+evolve`: regression red,
1/9 hidden. Это не provider/runner failure и не test interference, поэтому safe-retry policy не
разрешает перезапуск. Gate FAIL; 36-run full matrix не запускалась.

Post-run аудит также нашёл два raw `used_facts` без полного decision/path trace. Исходные rows
сохранены; gate и batch validation усилены так, чтобы новые эксперименты считали claimed-use
attribution complete только при непустом `decision` и `diff_paths` для каждого used ID.

## Hardening после canary

Ни одно изменение ниже не пересчитывает model output или quality score. Оно закрывает ложные
положительные gate/resume paths для будущих запусков:

- canary gate фиксирует exact tasks `a1,a4,a6`, три режима, seed1 и curator boundary;
- delete receipt требует `ok=true`, exact instance ID, правильную parent chain и
  `after_verified_clone`;
- precision read отвергает любые provider IDs вне локального reranked candidate set;
- solution/hidden-path leakage guard применяется ко всем origin, включая C0, до coding tokens;
- promotion требует точного normalized evidence-path match, не substring и не tests/runtime path;
- `used_facts` требуют decision + diff paths уже в Stop-hook и backend validator;
- reset не может удалить непустой lineage; clone verification failure делает scoped cleanup;
- dirty partial output без `--resume` запрещён, а technical failure обрывает затронутый memory
  stream вместо продолжения от invalid parent;
- completed-stream resume требует заново доказать exact chronology, verified clones, все scoped
  parent receipts и tail cleanup; одних `valid_run=true` rows недостаточно;
- curator session ID строится из фактической границы, а не hardcoded `a3`.

После hardening compile и полный бесплатный unit suite повторно запускаются. Отдельный post-run
[hardened gate audit](2026-09-02-sonnet5-xmemory-precision-v1-canary-gate-audit.json) на тех же
9 rows остаётся FAIL: shape/valid/preflight/pristine/precision/coverage/leakage/C0
green; paired/regression/claimed-use attribution red. Строгий chronological retention proof тоже
red: фактические 7 receipts корректны, но завершённый curator сохранил legacy session label
`evolve-after-a3` и не записал собственный parent-retention receipt в `evolution.json`. Новая версия
пишет динамический label и receipt; model rows ради этого не перезапускаются.

## Retention и data hygiene

Canary создал 7 xmemory children: 6 coding + curator. Все 7 реальных receipts имеют `ok=true` и
совпадающий scoped instance ID. Для 4/5 parent transitions row/evolution artifacts явно сохраняют
`after_verified_clone`; legacy curator transition имеет scoped lineage/delete receipt, но не этот
дополнительный proof. Tails удалены после окончания stream. C0 не удалялся и не мутировался;
digest до/после одинаков.

В git входят только runner/hardening code, Markdown и компактный CSV. Приватный `config.toml`,
credentials, raw Claude logs, workspaces, caches и `runs/` остаются gitignored.

## Открытые риски

1. Один repeat не отделяет memory effect от дисперсии coding-agent.
2. Evolve post-transfer task не получил ни одного learned fact, поэтому узкая learned-transfer
   гипотеза фактически не была активирована в этом stream.
3. `a6/on` получил один learned fact, но он был нерелевантен задаче и не claimed-used.
4. Strict C0 immutability сохраняет воспроизводимость, но learned fact может сообщить, что C0
   устарел относительно более позднего task base. Нужна явная temporal/base-commit semantics.
5. Precision/coverage измеряют отбор контекста, а не способность модели выбрать regression-safe
   реализацию; `a6` показал этот разрыв.
