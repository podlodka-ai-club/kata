#!/usr/bin/env python3
"""
Stop-хук: единственный хук, который умеет вернуть агента в работу (exit 2).

Сам он в память ничего не пишет — разбор «что изменилось» требует модели.
Хук лишь гарантирует, что шаг актуализации случится: возвращает агента
с инструкцией из usage-contract (U3) ровно один раз за сессию.

Гард от петли обязателен. Без маркера агент останавливается, хук его
возвращает, агент останавливается снова — и так до упора в лимит.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

MARKER = "memory_update_done.marker"

INSTRUCTION = """\
Шаг актуализации памяти (U3 из usage-contract).

Задача закончена. Прогони запись через гейт новизны и обнови память фактов:

1. Что из фактов, приехавших в начале сессии, разошлось с кодом — пометь stale
   со status_reason и заведи новый candidate. Код прав, память нет.
2. Что реально появилось в системе (endpoint, зависимость, событие, инвариант,
   настройка) — новые candidate-факты с evidence вида файл:строка.
3. На какие грабли наступил сам (упавшая сборка, поймавший регресс тест,
   требование проверки репозитория) — факт gotcha, только как гипотеза.
4. Оставь след задачи: объект Task со связями used_facts и produced_facts по fact_id.

Запиши один JSON-батч в `.kata-run/memory_mutations.json` (это `$KATA_RUN_DIR`). Формат:
{"mutations":[{"op":"create","fact":{"fact_id":"fact:gt-....","slice":"gotchas",
"statement":"...","content":"...","evidence":["файл:строка или лог"],"status":"candidate",
"confidence":"low","provenance":"inferred","source":"task"}},
{"op":"stale","fact_id":"fact:...","values":{"status_reason":"...","superseded_by":"fact:..."}},
{"op":"update","fact_id":"fact:...","values":{"...":"..."}},
{"op":"noop","fact_id":"fact:..."}],
"task":{"task_id":"<KATA_TASK_ID>","title":"...","used_facts":["fact:..."],
"produced_facts":["fact:..."],"decisions":[{"fact_id":"fact:...",
"decision":"какое решение в коде изменил прочитанный факт","diff_paths":["path/to/file.py"]}]}}.
Ссылаться в used_facts можно только на ID из стартового контекста. produced_facts — только ID
реальных create/update/stale этого батча. Если устойчивых изменений нет, mutations может быть
пустым, но Task и used_facts всё равно обязательны. Не переписывай соседние факты «заодно».
Runner проверит схему и применит её через настроенный backend после завершения этой сессии.

Когда закончишь — заверши сессию, повторно тебя не вернут.
"""


def main() -> int:
    run_dir = os.environ.get("KATA_RUN_DIR")
    if not run_dir:
        return 0  # вне раннера ничего не навязываем

    marker = Path(run_dir) / MARKER

    # Claude Code передаёт хуку JSON на stdin; нас интересует только защита
    # от повторного срабатывания внутри уже запущенного stop-hook.
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    if payload.get("stop_hook_active"):
        return 0

    if marker.exists():
        return 0

    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("1", encoding="utf-8")

    # exit 2 = не останавливаться, вернуть агента в работу; stderr уезжает ему в контекст
    instruction = INSTRUCTION.replace("<KATA_TASK_ID>", os.environ.get("KATA_TASK_ID", "unknown"))
    print(instruction, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
