# Canary: проблемы и изменения инфраструктуры запуска

Этот файл отделяет исследовательский результат от инженерной отладки. Ни один неуспешный или
ineligible результат не маскировался повтором ради улучшения метрик.

## xmemory и retention

- Организация имела жёсткий лимит в пять инстансов. После явного разрешения пользователя старые
  test-only instances удалены; C0 и чужие production-данные не затрагивались.
- Вместо опасного правила «удалить самый старый» runner удаляет только parent собственного stream
  после проверенного clone/read и tail после завершения. IDs, hashes и delete receipts остаются
  локально; semantic state локально не сохраняется.
- Первый provision C0 не прошёл проверку и был удалён. Успешный typed C0 сделан immutable и его
  digest проверялся после прогонов.
- xmemory отклонил зарезервированные timestamp fields — они удалены из seed payload.
- Provider relevance иногда возвращал пустой ответ. Retrieval переведён на детерминированное
  ранжирование кандидатов из remote state с последующим typed exact read.
- Один `MemoryState.snapshot_json` упёрся в provider string limit; zlib+base64 всё равно был велик.
  Добавлен digest-checked root manifest и canonical chunks по 6000 символов. Реальные a3/a6
  write→clone→read preflight прошли с совпадающими digests.

## Stop hook и batches

- Агент создавал неполные facts: отсутствовали evidence/обязательные поля. Hook теперь валидирует
  create/update/stale/noop и даёт один ограниченный repair turn.
- `stop_hook_active=true` ошибочно обходил проверку второго ответа; loop guard перенесён в
  persisted marker.
- Scoped `Write(./**)` не совпадал с абсолютным temp workspace path Claude Code; разрешение
  заменено на `Write`, при этом hook разрешает записать только ожидаемый batch по контракту runner.
- Были неподдерживаемые prefixes, слишком длинные IDs и коллизии IDs между задачами. Добавлена
  проверка `fact:xx-0000`, соответствия prefix/slice и временный ID-only registry. Он содержит
  только идентификаторы, не semantic memory.

## Claude/curator и orchestration

- Один coding call завершился `Connection closed mid-response`. Добавлен ровно один retry только
  для явных API/network failures и только при пустом source diff; usage попыток суммируется.
- Curator пытался менять структурные/typed поля, фактически retype facts. Prompt запрещает это,
  backend отклоняет structural/unknown fields и требует пометить ошибочно типизированный факт stale.
- Sweep после curator failure продолжал к a6. Исправлен fail-fast boundary: stream останавливается
  до следующей coding task.
- Resume раньше переисполнял только eligible cells, что позволяло selection bias. Теперь повторно
  используются все технически valid off-cells; частичный memory stream всегда начинается заново
  от C0, а valid ineligible rows сохраняются.
- Добавлены `--fail-fast`, completeness checks, retention metrics и проверки, что memory stream
  идёт строго хронологически без смешивания modes/seeds.

## Цена отладки

В retained canary вошли 6 coding runs + 1 curator. С учётом superseded технических попыток остались
22 coding metrics + 2 curator attempts общей расчётной стоимостью $84.783 против $29.461 в
финальном canary dataset. Основные причины — доработка cloud manifest, hook repair contract,
curator validation и transient network retry.
