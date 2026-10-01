#!/bin/bash
# Просмотр журналов сервиса.
#
# Журналы пишутся в двух местах, и это разные вещи:
#   logs/*        — журнал самого сервиса, полный и структурированный;
#   docker logs   — консольный поток контейнера, только INFO и выше.
# Разбор случая идёт по первому, проверка «поднялся ли контейнер» — по второму.

cd "$(dirname "$0")/.." || exit 1

MODE="${1:-app}"
LINES="${2:-100}"

usage() {
    cat <<'USAGE'
Использование: scripts/logs.sh [режим] [строк]

Режимы:
  app       ход работы сервиса (logs/app.log), по умолчанию
  errors    только ошибки и предупреждения (logs/errors.log)
  audit     аудит распознаваний (logs/audit.jsonl)
  docker    консольный поток контейнера
  follow    app.log в реальном времени

Примеры:
  scripts/logs.sh                 последние 100 записей хода работы
  scripts/logs.sh errors 50       последние 50 ошибок
  scripts/logs.sh audit           последние распознавания
  scripts/logs.sh follow          следить в реальном времени
USAGE
}

# jq делает JSON-строки читаемыми; без него показываем как есть.
pretty() {
    if command -v jq > /dev/null 2>&1; then
        # with_entries отбрасывает пустые ключи: в строке остаётся только то,
        # что действительно записано, иначе половина вывода — null.
        jq -c '{ts, level, event, request_id, field, seconds}
               | with_entries(select(.value != null))' 2>/dev/null || cat
    else
        cat
    fi
}

case "$MODE" in
    app)
        [ -f logs/app.log ] || { echo "logs/app.log не найден"; exit 1; }
        tail -n "$LINES" logs/app.log | pretty
        ;;
    errors)
        [ -f logs/errors.log ] || { echo "Ошибок нет: logs/errors.log не создан"; exit 0; }
        tail -n "$LINES" logs/errors.log
        ;;
    audit)
        [ -f logs/audit.jsonl ] || { echo "logs/audit.jsonl не найден — распознаваний ещё не было"; exit 0; }
        if command -v jq > /dev/null 2>&1; then
            tail -n "$LINES" logs/audit.jsonl \
                | jq -c '{ts, request_id, source_name, page, mode, schema, status, review_count, seconds: .duration_seconds}'
        else
            tail -n "$LINES" logs/audit.jsonl
        fi
        ;;
    docker)
        docker compose logs -f --tail="$LINES" margin
        ;;
    follow)
        [ -f logs/app.log ] || { echo "logs/app.log не найден"; exit 1; }
        tail -f logs/app.log | pretty
        ;;
    -h|--help|help)
        usage
        ;;
    *)
        echo "Неизвестный режим: $MODE"
        echo ""
        usage
        exit 1
        ;;
esac
