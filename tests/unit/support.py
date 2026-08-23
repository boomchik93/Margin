# -*- coding: utf-8 -*-
"""Общая подготовка модульных тестов.

Импортируется первой строкой каждого теста: модули сервиса читают пути из
переменных окружения в момент импорта, поэтому временные директории должны
быть выставлены раньше. Без этого тесты писали бы журнал и историю
распознаваний в рабочие каталоги проекта.
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

_TMP = tempfile.mkdtemp(prefix="ocr-tests-")

os.environ.setdefault("LOG_DIR", os.path.join(_TMP, "logs"))
os.environ.setdefault("RESULTS_DB", os.path.join(_TMP, "results.db"))
os.environ.setdefault("DICT_DIR", os.path.join(_TMP, "dictionaries"))
os.environ.setdefault("SCHEMA_DIR", os.path.join(ROOT, "config", "schemas"))
os.makedirs(os.environ["DICT_DIR"], exist_ok=True)


def tmpDir():
    return tempfile.mkdtemp(prefix="ocr-tests-")


def makeImage(path, size=(400, 300)):
    """Белая картинка с тёмной полосой — достаточно, чтобы пройти предобработку."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", size, "white")
    ImageDraw.Draw(img).rectangle(
        (40, 40, size[0] - 40, 60), fill=(20, 20, 120))
    img.save(path)
    return path


# Схема, на которой проверяется пайплайн: по одному полю каждого типа.
SAMPLE_SCHEMA = {
    "title": "Тестовая анкета",
    "language": "ru",
    "fields": [
        {"key": "title", "label": "Название", "type": "text", "case": "upper"},
        {"key": "city", "label": "Город", "type": "text",
         "dictionary": "cities"},
        {"key": "code", "label": "Код", "type": "text",
         "format": "[A-Z]{2}-\\d{4}"},
        {"key": "amount", "label": "Количество", "type": "number",
         "min": 0, "max": 100},
        {"key": "issued", "label": "Дата", "type": "date", "min_year": 2000},
        {"key": "phone", "label": "Телефон", "type": "phone"},
        {"key": "agree", "label": "Согласие", "type": "checkbox"},
    ],
}
