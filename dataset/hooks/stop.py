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
import re
import sys
from pathlib import Path

MARKER = "memory_update_done.marker"
FACT_ID_RE = re.compile(r"fact:[a-z]{2}-\d{4}")
PREFIX_SLICES = {"ac": "api-contracts", "iv": "invariants", "do": "data-ownership",
                 "cf": "config-flags", "gt": "gotchas"}
CREATE_REQUIRED = {"fact_id", "slice", "statement", "evidence", "status",
                   "confidence", "provenance", "source"}

INSTRUCTION = """\
Шаг актуализации памяти (U3 из usage-contract).

Задача закончена. Прогони запись через гейт новизны и обнови память фактов:

1. Факты с origin=c0 — неизменяемый стартовый слой: не update/stale их. Если код
   разошёлся с C0, создай отдельный learned candidate с evidence.
2. Что реально появилось в системе (endpoint, зависимость, событие, инвариант,
   настройка) — новые candidate-факты с evidence вида файл:строка.
3. Sandbox/tooling/pytest/permission-грабли не являются product architecture и в
   память этого precision-эксперимента не записываются.
4. Оставь след задачи: объект Task со связями used_facts и produced_facts по fact_id.

Перед выбором нового fact_id прочитай `.kata-hooks/existing-fact-ids.json`: там только занятые
ID текущего cloud state. Новый ID обязан иметь ровно четыре цифры и не совпадать ни с одним из них.

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
пустым, но Task и used_facts всё равно обязательны. Для каждого used_facts обязателен decisions
с непустыми decision и diff_paths; если факт не повлиял на код, не помечай его used.
Не переписывай соседние факты «заодно».
Runner проверит схему и применит её через настроенный backend после завершения этой сессии.

Перед завершением перечитай созданный JSON. У КАЖДОГО op=create, независимо от slice/status,
обязательны непустые fact_id, slice, statement, evidence (массив `файл:строка` или лог), status,
confidence, provenance и source. Evidence внутри prose/content не заменяет поле evidence.

Когда закончишь — заверши сессию, повторно тебя не вернут.
"""


def _injected_ids() -> set[str]:
    path = os.environ.get("KATA_FACTS_CONTEXT")
    if not path:
        return set()
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return {fact["fact_id"] for fact in payload.get("facts", []) if fact.get("fact_id")}
    except Exception:
        return set()


def _existing_ids() -> set[str]:
    path = os.environ.get("KATA_EXISTING_FACT_IDS")
    if not path:
        return set()
    try:
        values = json.loads(Path(path).read_text(encoding="utf-8"))
        return {value for value in values if isinstance(value, str)}
    except Exception:
        return set()


def validate_batch(path: Path, task_id: str) -> list[str]:
    try:
        batch = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ["memory_mutations.json отсутствует"]
    except Exception as exc:
        return [f"memory_mutations.json не является JSON: {exc}"]
    errors = []
    existing = _existing_ids()
    mutations = batch.get("mutations")
    if not isinstance(mutations, list):
        return ["mutations должен быть массивом"]
    changed = set()
    for index, item in enumerate(mutations):
        if not isinstance(item, dict) or item.get("op") not in {"create", "update", "stale", "noop"}:
            errors.append(f"mutations[{index}].op невалиден")
            continue
        op = item["op"]
        fact = item.get("fact") or {}
        fact_id = item.get("fact_id") or fact.get("fact_id")
        if not isinstance(fact_id, str) or not FACT_ID_RE.fullmatch(fact_id):
            errors.append(f"mutations[{index}].fact_id невалиден")
            continue
        prefix = fact_id.split(":", 1)[1].split("-", 1)[0]
        expected_slice = PREFIX_SLICES.get(prefix)
        if not expected_slice:
            errors.append(f"mutations[{index}] использует неподдерживаемый prefix {prefix}")
        elif op == "create" and fact.get("slice") != expected_slice:
            errors.append(
                f"mutations[{index}].slice должен быть {expected_slice} для prefix {prefix}")
        if op == "create" and fact_id in existing:
            errors.append(f"mutations[{index}] create конфликтует с занятым ID {fact_id}")
        if op != "noop":
            changed.add(fact_id)
        if op == "create":
            missing = sorted(key for key in CREATE_REQUIRED if not fact.get(key))
            if missing:
                errors.append(f"mutations[{index}] create не хватает {missing}")
            evidence = fact.get("evidence")
            if not isinstance(evidence, list) or not any(isinstance(ref, str) and ref.strip()
                                                         for ref in evidence):
                errors.append(f"mutations[{index}] create evidence должен быть непустым массивом")
        elif op in {"update", "stale"} and not isinstance(item.get("values"), dict):
            errors.append(f"mutations[{index}] {op} требует values object")
        if op == "stale" and not (item.get("values") or {}).get("status_reason"):
            errors.append(f"mutations[{index}] stale требует status_reason")
    task = batch.get("task") or {}
    if task.get("task_id") != task_id:
        errors.append(f"task.task_id должен быть {task_id}")
    used = task.get("used_facts")
    produced = task.get("produced_facts")
    if not isinstance(used, list) or not isinstance(produced, list):
        errors.append("task.used_facts/produced_facts должны быть массивами")
    else:
        injected = _injected_ids()
        if injected and set(used) - injected:
            errors.append("used_facts содержит ID вне стартового контекста")
        if set(produced) - changed:
            errors.append("produced_facts содержит ID без create/update/stale")
        decisions = task.get("decisions")
        if not isinstance(decisions, list):
            errors.append("task.decisions должен быть массивом")
        else:
            for fact_id in used:
                traced = [decision for decision in decisions
                          if isinstance(decision, dict)
                          and decision.get("fact_id") == fact_id
                          and isinstance(decision.get("decision"), str)
                          and decision["decision"].strip()
                          and isinstance(decision.get("diff_paths"), list)
                          and decision["diff_paths"]
                          and all(isinstance(value, str) and value.strip()
                                  for value in decision["diff_paths"])]
                if not traced:
                    errors.append(
                        f"used fact {fact_id} требует decisions с decision и diff_paths")
    return errors


def main() -> int:
    run_dir = os.environ.get("KATA_RUN_DIR")
    if not run_dir:
        return 0  # вне раннера ничего не навязываем

    marker = Path(run_dir) / MARKER

    # Claude Code marks the next Stop after an exit-2 continuation as
    # stop_hook_active=true. That flag is informational: returning early would skip
    # validation of the batch just requested by this hook. Our persisted attempt marker
    # is the actual hard loop guard.
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}

    if marker.exists():
        errors = validate_batch(Path(run_dir) / "memory_mutations.json",
                                os.environ.get("KATA_TASK_ID", "unknown"))
        if not errors:
            return 0
        try:
            attempt = int(marker.read_text(encoding="utf-8").strip())
        except Exception:
            attempt = 1
        if attempt >= 2:
            return 0  # hard loop guard; runner will reject and mark the row invalid
        marker.write_text("2", encoding="utf-8")
        print("Исправь только `.kata-run/memory_mutations.json`, код больше не меняй. "
              "Локальная проверка batch нашла:\n- " + "\n- ".join(errors) +
              "\nПеречитай JSON и заверши сессию ещё раз; runner выполнит окончательную проверку.",
              file=sys.stderr)
        return 2

    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("1", encoding="utf-8")

    # exit 2 = не останавливаться, вернуть агента в работу; stderr уезжает ему в контекст
    instruction = INSTRUCTION.replace("<KATA_TASK_ID>", os.environ.get("KATA_TASK_ID", "unknown"))
    print(instruction, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
