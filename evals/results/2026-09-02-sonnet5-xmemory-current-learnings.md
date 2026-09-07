# Что мы знаем после cloud xmemory canary и full r2

Этот документ отделяет подтверждённые факты от наблюдаемых сигналов и от гипотез, которые текущий
дизайн проверить не смог. Числа берутся из [full r2](2026-09-02-sonnet5-xmemory-full-r2.md) и
[canary](2026-09-01-sonnet5-xmemory-canary.md).

## Подтверждено экспериментом

1. **Cloud-only memory lifecycle работает.** Semantic state переживает независимые coding и
   curator sessions только через xmemory. Проверены read→write→clone→read, typed state, digest,
   curator boundary и scoped deletion. C0 не мутировался; все 31 runner-owned children canary+full
   удалены с receipts.
2. **Высокий retrieval coverage сам по себе недостаточен.** Во всех 24 full memory runs coverage
   равен 1.000, но median precision — 0.282. Ожидаемые факты приезжают, однако большая часть
   контекста нерелевантна конкретной задаче.
3. **Текущая память не дала устойчивого выигрыша на raw quality.** Full raw macro feature lift:
   off 0.556, on 0.479, evolve 0.514. Hidden totals: 194/222, 188/222, 192/222. Это secondary
   diagnostics, а не causal comparison, но преимущества memory modes в наблюдаемых данных нет.
4. **Эффект нестабилен между repeats.** On получил 91/111 hidden в seed1 и 97/111 в seed2 при
   неизменном протоколе. Два repeats не отделяют эффект памяти от model variance.
5. **Memory может сопровождаться вредным поведением.** В full отмечены 3 on и 2 evolve строки с
   `harmful_on_worse_off`; regression-after-retrieval — 6/12 в каждом memory mode. Stale facts не
   использовались, поэтому наблюдаемый риск связан не с известным stale-state, а с качеством или
   интерпретацией актуального контекста либо обычной model variance.
6. **Curator умеет обслуживать state, но его downstream benefit не доказан.** Он корректно делал
   noop, stale/update и дедупликацию gotchas, не меняя schema без оснований. После checkpoint нет
   устойчивого улучшения a4–a6 относительно on/off.
7. **Экономия не воспроизводится как общий эффект.** On был примерно на 5% дешевле off по
   расчётному dollar usage, но использовал немного больше output tokens. Evolve с curator был
   примерно на 7% дороже off и использовал примерно на 14% больше output tokens. Wall-clock
   сравнивать нельзя из-за suspend машины.

## Чего текущий эксперимент не доказал

- Нельзя утверждать, что memory причинно ухудшает или улучшает coding quality.
- Нельзя сравнивать primary macro 0.500 / 0.300 / 1.000: значения построены на разных задачах.
- Нельзя считать canary третьим repeat: в нём только a3/a6 и были инфраструктурные итерации.
- Нельзя приписать конкретному fact конкретную regression только по флагу attribution.

Причина: из 36 full rows eligible только 8. Нет ни одной task/seed ячейки, где eligible сразу все
три режима. Единственная paired eligible off/on ячейка — a3 seed2, lift 0.000 у обеих сторон.

## Рабочий вывод по продуктовой гипотезе

> Надёжная долговременная память не становится полезной автоматически. При precision около 0.28
> дополнительные факты скорее увеличивают пространство интерпретаций, чем дают агенту точную
> подсказку. Польза должна появляться из адресного retrieval и проверенных task-transfer lessons,
> а не из самого факта сохранения контекста.

Текущая версия гипотезы сужается до проверяемой формулировки:

> Небольшой набор высокоточных, подтверждённых прошлой задачей архитектурных фактов улучшает
> paired feature lift будущей задачи без regression и без увеличения process cost.

## Следующий матричный прогон

До траты Claude tokens следующий runner должен:

1. Физически запретить изменение и удаление существующих tests во время coding session, сохранив
   возможность добавлять новые tests; после сессии отдельно доказать pristine equality.
2. Считать primary только по **paired eligibility**: task/seed входит в comparison лишь когда все
   запрошенные режимы valid и eligible. Не усреднять разные наборы задач.
3. Добавить pre-paid canary, который обязан дать paired eligible строки, green regression и
   complete durability; при провале full не запускается.
4. Снизить retrieval payload: более строгий top-k/rerank и заранее логируемый relevance threshold.
   Сравнить текущий broad retrieval с precision-oriented memory, а не просто повторить r2.
5. Отделить **C0 architectural facts** от **learned transfer facts** и измерять, какие exact IDs
   использованы. Урок активируется только после evidence/check outcome; sandbox/tooling gotchas не
   должны попадать в product architecture context.
6. Для regression-red a1/a2 и adverse on seed1 до нового full сделать offline attribution review:
   injected fact → claimed use → diff path → regression failure. Future/hidden solution leakage
   остаётся запрещён.
7. Показать paired deltas по task/seed, bootstrap/range только после достаточного числа paired
   repeats, а также retrieval tokens, coding tokens, curator overhead и xmemory process calls.

Рекомендуемый gate: сначала a1/a4/a6 × все новые режимы × 1 repeat. Full запускать только если
каждая тройка paired eligible, regression green, existing tests untouched и cloud lineage complete.
