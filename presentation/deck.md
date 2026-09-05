---
marp: true
theme: default
size: 16:9
paginate: true
title: Системный дизайн как память агента
description: Хакатонный эксперимент с архитектурными фактами в xmemory
style: |
  section {
    background: #faf9f6;
    color: #202b35;
    font-family: Arial, sans-serif;
    font-size: 29px;
    line-height: 1.35;
    padding: 54px 66px 62px;
    justify-content: flex-start;
  }
  h1, h2 { color: #202b35; letter-spacing: -1.1px; }
  h1 { font-size: 61px; line-height: 1.12; margin: 38px 0 30px; }
  h2 { font-size: 43px; line-height: 1.15; margin: 0 0 30px; }
  p { margin: 0 0 22px; }
  strong { color: #175d69; }
  a { color: #175d69; text-decoration: underline; }
  ul, ol { margin: 0 0 20px; padding-left: 32px; }
  li { margin-bottom: 16px; }
  code { font-size: .83em; background: #e9eeec; }
  table { width: 100%; display: table; font-size: 27px; margin: 8px 0 24px; border-collapse: collapse; }
  th { color: #53636b; font-size: 23px; font-weight: 400; }
  th, td { border: 0; border-bottom: 1px solid #cbd4d2; padding: 14px 12px; }
  tr, tr:nth-child(2n) { background: transparent; }
  .source { position: absolute; bottom: 26px; left: 66px; right: 90px; font-size: 16px; color: #68757b; margin: 0; }
  .note { font-size: 23px; color: #53636b; }
  .lead { font-size: 36px; line-height: 1.3; }
  .columns { display: grid; grid-template-columns: 1fr 1fr; gap: 54px; }
  .stat { font-size: 84px; line-height: 1.05; font-weight: 700; color: #175d69; margin: 4px 0 14px; }
  .takeaway { border-top: 2px solid #175d69; padding-top: 20px; margin-top: 16px; font-size: 30px; }
  section.cover { justify-content: center; }
  section.cover h1 { margin-top: 0; }
  section.cover .note { margin-top: 36px; }
  section.ui .columns { grid-template-columns: 330px 1fr; gap: 32px; }
  section.ui img { width: 100%; height: 438px; object-fit: contain; object-position: top; }
  section.ui .columns p { font-size: 27px; }
  section::after { color: #68757b; font-size: 17px; right: 35px; bottom: 24px; }
---

<!-- _class: cover -->

# Системный дизайн<br>как память агента

<div class="lead">Поможет ли агенту база знаний<br>об архитектуре проекта?</div>

<p class="note">Hacker Sprint #2<br>Эксперимент с кодинговым агентом и xmemory</p>

---

## Гипотеза: меньше разбираться заново

Каждая новая сессия снова изучает проект.<br>Мы решили сохранить найденные правила и передавать их агенту.

<div class="columns">
<div>

**Например, задача**

Исправить поиск рецептов:<br>слова с дефисами не находятся.

</div>
<div>

**Что уже знает память**

Нормализованные поля вычисляются автоматически. Изменения требуют миграции данных.

</div>
</div>

<p class="takeaway">Ожидание: агент быстрее найдёт нужное место<br>и учтёт ограничения проекта.</p>

<p class="source">Реальный пример: задача a1 и факты do-0005/0006 из <a href="https://github.com/podlodka-ai-club/kata/blob/39d495b3329b355fbd2c54f97bea0dded15ca5c9/dataset/facts/snapshot-c0.md">стартовой памяти Mealie</a>.</p>

---

<!-- _class: ui -->

## Собрали скилл tech-facts

<div class="columns">
<div>

Человек выбирает,<br>что запоминать:<br>API, данные, зависимости.

Агент извлекает факты из кода. Сомнительные отдаёт на ревью.

В интерфейсе видны связи и ссылки на исходники.

</div>
<div>

![Эксплорер фактов и граф событий](./assets/ui-explore-events.png)

</div>
</div>

<p class="source">Интерфейс на демонстрационных данных services-platform, не на данных эвала Mealie. <a href="https://github.com/podlodka-ai-club/kata/tree/39d495b3329b355fbd2c54f97bea0dded15ca5c9/skills/tech-facts">Скилл и демо</a>.</p>

---

## Память между задачами

**В xmemory:** типы фактов, сущности и связи между ними.<br>У факта есть источник в коде, статус и степень уверенности.

1. Перед задачей агент получает подходящие факты.
2. После правок обновляет память и сохраняет новые наблюдения.
3. Между задачами отдельная сессия убирает дубли<br>и помечает устаревшие записи.

<p class="takeaway">Для Mealie начали с <strong>19 фактов</strong>: API, ограничения,<br>владение данными и настройки.</p>

<p class="source">Схема под задачу и чтение после записи входят в основной сценарий. <a href="https://github.com/podlodka-ai-club/kata/blob/2f82aca3bd0f3bb96c2b39953686adb7df9081d5/dataset/runner/README.md">Протокол и раннер</a>.</p>

---

## Как проверяли пользу

**Mealie, 6 реальных задач:** от исправления поиска<br>до входа через OIDC. Claude Sonnet 5, effort medium.

| Режим | Что получает агент |
| --- | --- |
| Без памяти | Код и постановку задачи |
| С памятью | Ещё и факты, которые обновляет после работы |
| С пересмотром памяти | То же + отдельную сессию ревизии фактов |

Проверяли новые функции скрытыми тестами из реальных PR<br>и отдельно проверяли, не сломалось ли старое поведение.

<p class="source">На каждую задачу новая сессия и чистая копия кода. Тесты проверены на коде до и после эталонного исправления. <a href="https://github.com/podlodka-ai-club/kata/blob/2f82aca3bd0f3bb96c2b39953686adb7df9081d5/dataset/tasks.yaml">Задачи</a>.</p>

---

## Сначала увидели экономию токенов

<p class="note">Первая проверка: один и тот же текст из 19 фактов для всех задач. 12 запусков.</p>

<div class="columns">
<div>

<div class="stat">−19%</div>

выходных токенов с памятью

205 тыс. без памяти<br>166 тыс. с памятью

</div>
<div>

**Но 65% этой разницы<br>дала одна задача.**

На ней агент с памятью прошёл<br>1 из 9 проверок вместо 3 из 9<br>и сломал прежние тесты.

</div>
</div>

<p class="takeaway">Меньший расход ещё не означает эффективность.<br>Нужно смотреть, сколько работы агент выполнил.</p>

<p class="source"><a href="https://github.com/podlodka-ai-club/kata/blob/39d495b3329b355fbd2c54f97bea0dded15ca5c9/evals/results/2026-09-01-sonnet5-medium-seed1.md">Первый прогон</a> и <a href="https://github.com/podlodka-ai-club/kata/blob/d2cf2a4d656b95b0f78425eb4344f9987d97aa31/evals/results/2026-09-01-analysis.md">разбор экономии</a>. Один повтор, причинный эффект не установлен.</p>

---

## В большом прогоне перевеса не было

<p class="note">Живое чтение и запись в xmemory. 6 задач × 3 режима × 2 повтора = 36 запусков.</p>

| Пройденные скрытые проверки | Повтор 1 | Повтор 2 |
| --- | ---: | ---: |
| Без памяти | 97 / 111 | 97 / 111 |
| С памятью | 91 / 111 | 97 / 111 |
| С пересмотром памяти | 97 / 111 | 95 / 111 |

**Это наблюдения, а не доказательство вреда памяти.**

По правилам отбора пригодны лишь 8 из 36 запусков:<br>агенты меняли существующие тесты или ломали старое поведение.

<p class="source"><a href="https://github.com/podlodka-ai-club/kata/blob/2f82aca3bd0f3bb96c2b39953686adb7df9081d5/evals/results/2026-09-02-sonnet5-xmemory-full-r2.md">Полный прогон</a>. Таблица включает все запуски. Ни одной пригодной тройки режимов для одной задачи и повтора.</p>

---

## Убрали лишние факты. Качество то же

<p class="note">Следующая проверка: запретили менять старые тесты и сузили выдачу памяти. 9 запусков.</p>

**Доля подходящих фактов выросла с 28% до 100%**<br>по медиане. Объём подсказки сократился примерно втрое.

| Проверки новой функции | Без памяти | С памятью | С ревизией |
| --- | ---: | ---: | ---: |
| Поиск рецептов | 6 / 6 | 6 / 6 | 6 / 6 |
| Вход через OIDC | 3 / 5 | 3 / 5 | 3 / 5 |

На третьей задаче режим с ревизией сломал старое поведение.<br>Большой повторный эксперимент остановили.

<p class="source"><a href="https://github.com/podlodka-ai-club/kata/blob/2f82aca3bd0f3bb96c2b39953686adb7df9081d5/evals/results/2026-09-02-sonnet5-xmemory-precision-v1-canary.md">Проверка точного отбора</a>. Один повтор. Третья задача исключена из сопоставления всех режимов.</p>

---

## Новые записи ещё не стали опытом

В большом прогоне все 24 сессии с памятью получали<br>только исходные факты. Новые уроки не попадали в выдачу.

**В следующей проверке один новый факт всё же дошёл:**<br>урок о нормализации текста попал в задачу про счётчики рецептов.

Агент не отметил его как использованный.<br>Результат совпал с вариантом без памяти.

<p class="takeaway">Мы подтвердили сохранение знаний между сессиями.<br>Пользу переноса урока на следующую задачу пока не показали.</p>

<p class="source"><a href="https://github.com/podlodka-ai-club/kata/blob/2f82aca3bd0f3bb96c2b39953686adb7df9081d5/evals/results/2026-09-02-sonnet5-xmemory-precision-v1-attribution.md">Разбор использования фактов</a>. Факт do-0007 в a6/on. В режиме с ревизией новые факты не прошли отбор.</p>

---

## Реляционная память добавила работы

<p class="lead">Чтобы агент получил короткую подсказку,<br>пришлось обслуживать целую модель проекта.</p>

| Что хотели получить | Что пришлось поддерживать |
| --- | --- |
| Правило из кода | Типы объектов, ключи и связи |
| Актуальную подсказку | Статусы, дубли и обновления после правок |
| Знание для новой задачи | Собственный отбор фактов и проверку их пользы |

<p class="takeaway">Наш вывод: для этой задачи сложность реляционной<br>памяти пока не оправдалась результатом.</p>

<p class="source">Инженерный вывод по этому прототипу. С Markdown-памятью и другими хранилищами не сравнивали. <a href="https://github.com/podlodka-ai-club/kata/blob/39d495b3329b355fbd2c54f97bea0dded15ca5c9/skills/tech-facts/memory-protocol.md">Протокол памяти</a>.</p>

---

## Что пришлось учитывать в xmemory

**Запись текстом могла придумать структуру.**<br>При обкатке получили 20 лишних связей и объект с пустым ключом.<br>Перешли на явные структурированные изменения через API.

**Выдачу под задачу пришлось делать самим.**<br>Команда `context --text` возвращала общий контекст.<br>Добавили собственное ранжирование поверх сырых данных.

**Историю операций собирали в раннере.**<br>Логировали, что прочитали и записали. Денежную стоимость<br>операций памяти CLI не сообщал.

<p class="source">Наблюдения за закрытой бетой в августе–сентябре 2026, не описание текущих гарантий продукта. <a href="https://github.com/podlodka-ai-club/kata/blob/39d495b3329b355fbd2c54f97bea0dded15ca5c9/skills/tech-facts/memory-protocol.md">Протокол</a>, <a href="https://github.com/podlodka-ai-club/kata/blob/2f82aca3bd0f3bb96c2b39953686adb7df9081d5/evals/results/2026-09-01-sonnet5-xmemory-canary-problems.md">журнал проблем</a>.</p>


---

## Что показал эксперимент

**Память между сессиями работает.**<br>Собрали скилл, интерфейс и цикл чтения и обновления фактов.

**Преимущества в решении задач не увидели.**<br>Даже после улучшения отбора фактов результаты<br>на двух сопоставимых задачах остались прежними.

**Обслуживание памяти стало отдельной работой.**<br>Пришлось поддерживать схему, связи, актуальность и отбор фактов.

<p class="takeaway">Для нашего кодингового агента сложность реляционной<br>памяти пока не оправдалась результатом.</p>

<p class="source">Вывод по этому прототипу, без сравнения с другими хранилищами. <a href="https://github.com/podlodka-ai-club/kata">Репозиторий</a> · <a href="https://github.com/podlodka-ai-club/kata/pull/9">Результаты эксперимента</a>.</p>
