#!/bin/bash
# Остановка сервиса.

cd "$(dirname "$0")/.." || exit 1

docker compose down

echo "Сервис остановлен."
echo "Удалить образ: docker rmi margin:latest"
