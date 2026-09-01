#!/usr/bin/env bash
# SessionStart: stdout этого хука уезжает в контекст сессии.
# Это единственный канал, через который память попадает к агенту в режиме memory-on.
#
# Primary runtime receives a task-scoped context prepared by the backend adapter.
# The adapter logs the exact provider response and cost before this hook injects it.
# `snapshot-naive` is retained only to reproduce the old full-dump negative control.
set -uo pipefail

OUT=""

# След того, что хук вообще стрелял. Без него «память не приехала» неотличимо
# от «хук не сработал», и разбор красного прогона превращается в гадание.
if [[ -n "${KATA_RUN_DIR:-}" ]]; then
  mkdir -p "$KATA_RUN_DIR"
  : > "$KATA_RUN_DIR/context_injected.txt"
  date -Iseconds > "$KATA_RUN_DIR/hook_session_start.fired"
fi

case "${KATA_MEMORY_MODE:-prepared}" in
  prepared)
    if [[ -r "${KATA_FACTS_CONTEXT:-}" ]]; then
      OUT="$(cat "$KATA_FACTS_CONTEXT")"
    else
      echo "kata: prepared context не найден: ${KATA_FACTS_CONTEXT:-<пусто>}" >&2
      exit 1    # раннер увидит пустой context_injected.txt и пометит прогон невалидным
    fi
    ;;
  snapshot-naive)
    if [[ -r "${KATA_FACTS_SNAPSHOT:-}" ]]; then
      OUT="$(cat "$KATA_FACTS_SNAPSHOT")"
    else
      echo "kata: снапшот фактов не найден: ${KATA_FACTS_SNAPSHOT:-<пусто>}" >&2
      exit 1
    fi
    ;;
  *)
    echo "kata: неизвестный KATA_MEMORY_MODE=${KATA_MEMORY_MODE}" >&2
    exit 1
    ;;
esac

# Ровно то, что уехало в контекст, кладём рядом с прогоном — без этого
# attribution «факт → пройденная проверка» не собрать.
if [[ -n "${KATA_RUN_DIR:-}" ]]; then
  printf '%s' "$OUT" > "$KATA_RUN_DIR/context_injected.txt"
fi

if [[ -z "${OUT// }" ]]; then
  echo "kata: контекст пуст — memory-on выродится в memory-off" >&2
  exit 1
fi

cat <<EOF
<project-facts source="tech-facts" mode="${KATA_MEMORY_MODE:-prepared}">
Ниже — известные технические факты об этом проекте, извлечённые из его кода.
Каждый факт имеет evidence (файл:строка). Код прав, память — нет: если факт
расходится с кодом, следуй коду и отметь расхождение в итоговом отчёте.

$OUT
</project-facts>
EOF
