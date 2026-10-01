#!/bin/bash
# Запуск Margin в Docker.

# Скрипт лежит в scripts/, а docker-compose.yml и конфиги — в корне проекта.
cd "$(dirname "$0")/.." || exit 1

set -e

if ! command -v docker &> /dev/null; then
    echo "Ошибка: Docker не установлен"
    exit 1
fi

if ! docker compose version &> /dev/null; then
    echo "Ошибка: Docker Compose не установлен"
    exit 1
fi

# Каталоги томов. Внутри контейнера сервис работает под uid 1000; если ваш
# uid другой, выдайте права: sudo chown -R 1000:1000 logs data uploads
mkdir -p models uploads logs logs/raw data/dictionaries data/in/archive data/out

if [ ! -f "config/settings.json" ]; then
    cp config/settings.example.json config/settings.json
    echo "Создан config/settings.json из примера"
fi

if [ ! -f "models/Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf" ]; then
    echo "Внимание: файл модели не найден в models/"
    echo "Сервис поднимется, но распознавание вернёт 503. Как скачать — в README."
fi

# Проверка синтаксиса схем и словарей до запуска: битый файл не уронит
# сервис, но и не загрузится, и обнаружится это только по результату.
for f in config/schemas/*.json data/dictionaries/*.json; do
    [ -e "$f" ] || continue
    if ! python3 -m json.tool "$f" > /dev/null 2>&1; then
        echo "Внимание: $f — некорректный JSON, файл не загрузится"
    fi
done

echo "Сборка и запуск..."
docker compose up -d --build

sleep 5
docker compose ps

PORT="${SERVER_PORT:-5002}"

echo ""
echo "Журналы:       docker compose logs -f   (файлы — в logs/)"
echo "Готовность:    curl http://localhost:$PORT/api/status"
echo "Схемы:         curl http://localhost:$PORT/api/schemas"
echo "Веб-интерфейс: http://localhost:$PORT"
echo "Swagger:       http://localhost:$PORT/apidocs/"
