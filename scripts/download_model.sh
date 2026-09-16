#!/bin/bash
# Скачивание модели и проектора в models/ (около 6 ГБ).
#
# Файлы берутся из репозитория unsloth/Qwen2.5-VL-7B-Instruct-GGUF на
# Hugging Face и сохраняются под теми именами, которые ждёт сервис
# (config/settings.json -> "пути").

cd "$(dirname "$0")/.." || exit 1

set -e

REPO="unsloth/Qwen2.5-VL-7B-Instruct-GGUF"
MODEL_SRC="Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf"
MMPROJ_SRC="mmproj-F16.gguf"
MODEL_DST="models/Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf"
MMPROJ_DST="models/mmproj-Qwen2.5-VL-7B-Instruct-f16.gguf"

mkdir -p models

if ! python3 -c "import huggingface_hub" 2> /dev/null; then
    echo "Нужен пакет huggingface-hub: pip install huggingface-hub"
    exit 1
fi

if [ ! -f "$MODEL_DST" ]; then
    echo "Скачивание модели (~4.7 ГБ)..."
    python3 -c "from huggingface_hub import hf_hub_download; hf_hub_download('$REPO', '$MODEL_SRC', local_dir='models')"
else
    echo "Модель уже на месте: $MODEL_DST"
fi

if [ ! -f "$MMPROJ_DST" ]; then
    echo "Скачивание проектора (~1.35 ГБ)..."
    python3 -c "from huggingface_hub import hf_hub_download; hf_hub_download('$REPO', '$MMPROJ_SRC', local_dir='models')"
    mv "models/$MMPROJ_SRC" "$MMPROJ_DST"
else
    echo "Проектор уже на месте: $MMPROJ_DST"
fi

echo "Готово:"
ls -lh "$MODEL_DST" "$MMPROJ_DST"
