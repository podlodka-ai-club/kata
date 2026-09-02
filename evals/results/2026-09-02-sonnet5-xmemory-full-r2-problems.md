# Full r2: проблемы прогона и инфраструктурные замечания

## Во время full matrix

1. **Sleep/suspend исказил wall-clock.** Машина засыпала во время seed2. `ps ELAPSED` включил
   suspend-time, тогда как timeout runner основан на active monotonic time. Сессия a3/on/seed2
   поэтому визуально превысила час, но штатно завершилась и прошла memory/test checks. Wall-clock
   нельзя использовать для сравнения режимов в этом прогоне.
2. **Один transient Claude failure.** Первая попытка a6/off/seed2 вернула
   `API Error: Connection closed mid-response` при пустом source diff. Разрешённый bounded retry
   2/2 завершился успешно; row valid, `agent_attempts=2`, `agent_transient_retries=1`, usage/cost
   обеих попыток суммирован ($6.594 для строки). Других retry не было.
3. **Eligibility collapse.** 28/36 строк исключены из primary: 22 затронули существующие tests,
   12 имели regression red, с пересечением в 6 строк. Это не технический сбой runner: scoring шёл
   на восстановленном pristine tests tree, а gate сработал как был объявлен. Но итоговая матрица
   не содержит полностью paired eligible сравнения трёх режимов.
4. **Adverse memory diagnostics.** On seed1 дал 91/111 hidden против 97/111 off и четыре красные
   regression. В seed2 raw hidden сравнялся с off, поэтому результат нестабилен и требует ручной
   attribution-разборки, а не автоматического rerun.

## Инфраструктурные изменения

Новых изменений runner после canary checkpoint `112d729` во время full не понадобилось. Full
использовал уже зафиксированные механизмы:

- cloud manifest root + digest-checked chunks;
- exact typed retrieval поверх remote state;
- один safe retry только при explicit transient error и пустом diff;
- strict batch/ID validation и один repair turn Stop hook;
- structural guards curator;
- resume без selection bias;
- scoped deletion verified parent/tail вместо глобального «самого старого» instance.

Подробная история проблем, которые были исправлены до full, находится в
[canary infrastructure report](2026-09-01-sonnet5-xmemory-canary-problems.md).

## Retention и данные

Full создал 26 runner-owned children и удалил все 26 после verified clone/read либо завершения
stream. C0 не мутировался. В git добавляются только агрегированный Markdown и компактный CSV;
agent stdout/stderr, workspaces, приватный `config.toml`, credentials и большие raw caches остаются
gitignored. Canary сохранён отдельными файлами и не смешан с full r2.
