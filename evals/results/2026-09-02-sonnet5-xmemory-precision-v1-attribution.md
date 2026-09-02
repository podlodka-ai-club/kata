# Attribution review: full r2 adverse rows и precision-v1 canary

Разбор выполнен по сохранённым `context_injected.txt`, `attribution.json`, `diff.patch`,
`metrics.json`, regression/hidden logs и memory mutations. Причинная граница строгая:

> Outcome после retrieval не доказывает влияние памяти. Fact-level association требует exact
> injected ID/content, claimed use и decision path, который присутствует в actual diff. Даже такая
> связь остаётся ассоциацией, если факт не предписывает конкретный дефект.

## Ограничение attribution full r2

Старый runner записывал полный список row `diff_paths` на каждый injected fact. Поэтому это поле
само по себе не является trace использования. В review учитывались только `Task.used_facts` и
`code_decision.diff_paths`. Во всех 24 memory rows full r2 были инжектированы только C0 facts;
learned/gotcha facts писались, но ни разу не извлекались. Следовательно, full r2 не позволяет
делать выводы о learned transfer.

## Full r2: regression/adverse review

Для `a1` все четыре memory rows получили одинаковые десять C0 IDs:
`do-0001..6`, `iv-0001..4`; во всех claimed-used были `do-0005`, `do-0006`. Только
`a1/on/s1` связал оба факта исключительно с `query_search.py` и получил punctuation miss.
Остальные rows также имели три scheduler timeline regression failures, но их claimed paths
(`_model_base.py`, `query_search.py`, migration) не пересекают scheduler.

Для `a2` все четыре memory rows получили одинаковые 13 C0 IDs: `ac-0001`, `ac-0002`,
`ac-0004`, `cf-0001..6`, `iv-0001..4`. Claimed-used subsets были:

- on/s1: `cf-0005 → tests/unit_tests/test_config.py`;
- on/s2: `ac-0001 → media_recipe.py`, `cf-0001 → app_about.py/about schema/frontend type`,
  `cf-0002 → settings.py`;
- evolve/s1: `ac-0001 → app_about.py/about schema`, `cf-0002 → settings.py`,
  `cf-0005 → test_config.py`;
- evolve/s2: `cf-0002 → settings.py`, `cf-0006 → backend-config.md`.

Каждая a2 memory row получила одни и те же три scheduler timeline regression failures. Ни один
claimed path не относится к scheduler; эти failures остаются unattributed. Различные 42/44 или
43/44 hidden outcomes связаны с неполными asset/host/default-extension решениями, которых injected
facts не описывали.

| Row / факт | Claimed trace | Outcome / фактический дефект | Граница вывода |
| --- | --- | --- | --- |
| `a1/on/s1`, `do-0005`, `do-0006` | `query_search.py` | punctuation regression и 3 hidden punctuation misses | strongest direct association; другие repeats с теми же facts выбрали противоположную реализацию, причинность не установлена |
| `a2` memory rows | settings/testing/docs paths; нет пересечения с scheduler | повторные timeline regression failures; feature misses вокруг asset hardening и host/default extension | failures не атрибутированы injected facts |
| `a3/on/s1` | нет path intersection | timeline regression family | не атрибутировано |
| `a4/on/s1`, `ac-0001` | auth route path | 2/5; отсутствующий OIDC должен давать 404, реализация отклоняла запрос иначе | defect не содержится в факте |
| `a5/on/s1` | config/API paths | 5/5, regression green | полезный non-adverse contrast, но не доказательство causal benefit |
| `a6/on/s1`, `do-0003` и C0 | category/tag models и repository | broad SQL auto-correlation regression, 1/9 hidden | strong diff→failure trace; fact→defect weak |

Самый сильный старый adverse trace `a6/on/s1` воспроизводит тот же класс ошибки, что новый
canary row ниже: `column_property` subquery для recipe count без явной correlation.

## Precision-v1 canary: exact retrieval

| Task/mode | Injected IDs и origin | Claimed-used trace |
| --- | --- | --- |
| a1/on | `do-0005`, `do-0006` C0 | оба имеют decision paths; два learned facts подтверждены checks |
| a1/evolve | `do-0005`, `do-0006` C0 | один claimed-use trace hole остался в raw row |
| a4/on | `ac-0001`, `ac-0002`, `iv-0001` C0 | только `ac-0001 → auth.py`; остальные не claimed-used |
| a4/evolve | те же три C0 | два claimed-use paths; a4 candidates остались inactive |
| a6/on | `ac-0002`, `do-0001..3` C0 + `do-0007` learned | два C0 traces; `do-0007` не claimed-used |
| a6/evolve | `ac-0002`, `do-0001..3` C0 | `ac-0002 → controllers`, `do-0003 → repository_factory.py` |

Два raw trace holes (`a1/evolve do-0006`, `a6/on do-0002`) означают, что факт был указан в
`used_facts`, но для него нет полного decision/diff path. Они не используются для causal claims;
после canary validator и gate требуют trace для каждого claimed-used ID.

## Adverse `a6/on+evolve`

`memory-off` и precision `memory-on` прошли regression (766 passed, 6 skipped) и получили 3/9
hidden. Precision `memory-on+evolve` technically valid, но regression-red: 726 passed, 30 failed,
10 errors, 6 skipped; hidden 1/9. Existing tests во всех трёх строках pristine.

Evolve row получил только C0 `ac-0002`, `do-0001`, `do-0002`, `do-0003`. Ни один learned fact
после curator не был выбран. `ac-0002` заявлен как основание новых методов существующих
class-based controllers (`controller_categories.py`, `controller_tags.py`); `do-0003` — как
основание scoped repository paths (`repository_factory.py`). `do-0001` и `do-0002` не отмечены
использованными.

Фактический regression defect находится вне claimed paths:

- `mealie/db/models/recipe/category.py` добавил `Category.recipe_count` через scalar subquery;
- `mealie/db/models/recipe/tag.py` добавил аналогичный `Tag.recipe_count`;
- обе subquery не задали явную correlation;
- SQLAlchemy сообщил `returned no FROM clauses due to auto-correlation`, затем Pydantic не смог
  прочитать `recipe_category`/`tags`; это распространилось на repository, scheduler и hidden API.

Off-решение использовало такие же model properties, но с явной correlation и прошло regression.
On-решение считало count в repository и также прошло. В full r2 `a6/on/s1` тот же дефект дал
тот же набор 30 failed + 10 errors и 1/9; при этом `a6/evolve/s1` с теми же C0 facts использовал
корректную correlation и прошёл regression. Это указывает на нестабильность выбранной реализации,
а не на детерминированное действие факта или режима.

## Что можно и нельзя заключить

Можно:

- новый reranker materially поднял retrieval precision при полной coverage;
- высокая retrieval precision сама по себе не гарантирует regression-safe реализацию;
- текущий adverse row вызван конкретной SQL correlation ошибкой;
- paired gate правильно исключил всю `a6/seed1` ячейку и остановил full;
- подтверждённого learned-fact use будущей задачей не наблюдалось.

Нельзя:

- утверждать, что regression вызвали curator, evolve или learned memory;
- приписывать дефект `ac-0002`/`do-0003`: их claimed paths не пересекают model defect;
- трактовать raw `a6` delta как primary memory effect;
- считать full r2 evidence learned transfer: learned facts там не извлекались.
