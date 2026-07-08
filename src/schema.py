# -*- coding: utf-8 -*-
"""Схемы полей: описание того, что извлекать из документа.

Схема — JSON-файл в config/schemas/. Имя файла без расширения служит именем
схемы: `config/schemas/example_form.json` вызывается как `schema=example_form`.
Из схемы собираются промпты модели, пустой результат и правила нормализации,
поэтому добавить новый вид документа можно без правки кода.

Формат описан в docs/SCHEMAS.md.
"""
import json
import os
import re
import threading

from logging_setup import getLogger

log = getLogger("schema")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Директория схем. Переопределяется SCHEMA_DIR — в Docker её удобно
# монтировать томом и править схемы без пересборки образа.
SCHEMA_DIR = os.environ.get("SCHEMA_DIR") or os.path.join(
    PROJECT_ROOT, "config", "schemas")

FIELD_TYPES = ("text", "number", "date", "phone", "checkbox")
CASES = ("keep", "upper", "lower")

# Три значения вместо булева: "unclear" — отдельный исход. Булево не отличает
# «отметки нет» от «не разглядел», а это разные случаи для того, кто
# проверяет результат.
CHECKED = "checked"
UNCHECKED = "unchecked"
UNCLEAR = "unclear"
CHECKBOX_VALUES = (CHECKED, UNCHECKED, UNCLEAR)

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

MAX_FIELDS = 100
MAX_ZONES = 30
# Сколько раз читается зона с "vote": true. Двух голосов мало: они либо
# совпадают, либо расходятся, и выбрать не из чего. С тремя большинство есть
# в каждом символе.
VOTE_PASSES = 3


# === ПРОВЕРКА ===

def _box(value, where, errors):
    """Прямоугольник в долях страницы (x0, y0, x1, y1) или None."""
    if (not isinstance(value, (list, tuple)) or len(value) != 4
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                       for v in value)):
        errors.append("%s: box должен быть списком из четырёх чисел "
                      "[x0, y0, x1, y1]" % where)
        return None
    x0, y0, x1, y1 = (float(v) for v in value)
    if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0):
        errors.append("%s: box задаётся в долях страницы 0..1, "
                      "левый верхний угол раньше правого нижнего" % where)
        return None
    return [x0, y0, x1, y1]


def _number(value, where, name, errors):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append("%s: %s должно быть числом" % (where, name))
        return None
    return value


def _field(raw, index, seen, errors):
    where = "fields[%d]" % index
    if not isinstance(raw, dict):
        errors.append("%s: ожидается объект" % where)
        return None

    key = raw.get("key")
    if not isinstance(key, str) or not KEY_RE.match(key):
        errors.append("%s: key обязателен — латиница в нижнем регистре, "
                      "цифры и подчёркивание, начинается с буквы" % where)
        return None
    where = "поле %s" % key
    if key in seen:
        errors.append("%s: ключ повторяется" % where)
        return None
    seen.add(key)

    ftype = raw.get("type", "text")
    if ftype not in FIELD_TYPES:
        errors.append("%s: неизвестный type %r, допустимы %s"
                      % (where, ftype, ", ".join(FIELD_TYPES)))
        return None

    field = {
        "key": key,
        "label": str(raw.get("label") or key).strip(),
        "type": ftype,
        "hint": str(raw.get("hint") or "").strip(),
        "required": bool(raw.get("required", False)),
    }

    if ftype == "text":
        case = raw.get("case", "keep")
        if case not in CASES:
            errors.append("%s: case должен быть одним из %s"
                          % (where, ", ".join(CASES)))
            case = "keep"
        field["case"] = case

        pattern = raw.get("format")
        if pattern is not None:
            try:
                re.compile(str(pattern))
                field["format"] = str(pattern)
            except re.error as e:
                errors.append("%s: format не разбирается как регулярное "
                              "выражение (%s)" % (where, e))

        dictionary = raw.get("dictionary")
        if dictionary is not None:
            if isinstance(dictionary, str) and NAME_RE.match(dictionary):
                field["dictionary"] = dictionary
                max_dist = _number(raw.get("max_dist"), where, "max_dist",
                                   errors)
                if max_dist is not None:
                    if max_dist <= 0:
                        errors.append("%s: max_dist должен быть больше нуля"
                                      % where)
                    else:
                        field["max_dist"] = float(max_dist)
            else:
                errors.append("%s: dictionary — имя файла словаря без "
                              "расширения" % where)

    elif ftype == "number":
        for name in ("min", "max"):
            value = _number(raw.get(name), where, name, errors)
            if value is not None:
                field[name] = value

    elif ftype == "date":
        for name in ("min_year", "max_year"):
            value = _number(raw.get(name), where, name, errors)
            if value is not None:
                field[name] = int(value)

    elif ftype == "phone":
        digits = _number(raw.get("digits"), where, "digits", errors)
        if digits is not None:
            field["digits"] = int(digits)

    return field


def _zone(raw, index, keys, seen, errors):
    where = "zones[%d]" % index
    if not isinstance(raw, dict):
        errors.append("%s: ожидается объект" % where)
        return None

    name = raw.get("name") or "zone%d" % (index + 1)
    if not isinstance(name, str) or not NAME_RE.match(name):
        errors.append("%s: name — латиница, цифры, дефис и подчёркивание"
                      % where)
        return None
    where = "зона %s" % name
    if name in seen:
        errors.append("%s: имя повторяется" % where)
        return None
    seen.add(name)

    box = _box(raw.get("box"), where, errors)

    fields = raw.get("fields")
    if (not isinstance(fields, list) or not fields
            or not all(isinstance(f, str) for f in fields)):
        errors.append("%s: fields — непустой список ключей полей" % where)
        return None
    unknown = [f for f in fields if f not in keys]
    if unknown:
        errors.append("%s: в схеме нет полей %s"
                      % (where, ", ".join(unknown)))
        return None

    vote = bool(raw.get("vote", False))
    if vote and len(fields) != 1:
        errors.append("%s: vote работает только для зоны с одним полем"
                      % where)
        vote = False
    if vote and keys[fields[0]]["type"] == "checkbox":
        errors.append("%s: vote не применяется к отметкам" % where)
        vote = False

    if box is None:
        return None
    return {
        "name": name,
        "box": box,
        "fields": list(fields),
        "hint": str(raw.get("hint") or "").strip(),
        "override": bool(raw.get("override", False)),
        "vote": vote,
    }


def validate(raw, name="inline"):
    """Проверяет описание схемы. Возвращает (схема, ошибки).

    При непустом списке ошибок схема равна None: работать по наполовину
    разобранной схеме значило бы молча терять поля.
    """
    errors = []
    if not isinstance(raw, dict):
        return None, ["схема должна быть JSON-объектом"]

    raw_fields = raw.get("fields")
    if not isinstance(raw_fields, list) or not raw_fields:
        return None, ["fields: нужен непустой список полей"]
    if len(raw_fields) > MAX_FIELDS:
        return None, ["fields: не больше %d полей" % MAX_FIELDS]

    seen = set()
    fields = []
    for i, item in enumerate(raw_fields):
        field = _field(item, i, seen, errors)
        if field is not None:
            fields.append(field)
    keys = {f["key"]: f for f in fields}

    zones = []
    raw_zones = raw.get("zones") or []
    if not isinstance(raw_zones, list):
        errors.append("zones: ожидается список")
        raw_zones = []
    if len(raw_zones) > MAX_ZONES:
        errors.append("zones: не больше %d зон" % MAX_ZONES)
        raw_zones = []
    seen_zones = set()
    for i, item in enumerate(raw_zones):
        zone = _zone(item, i, keys, seen_zones, errors)
        if zone is not None:
            zones.append(zone)

    if errors:
        return None, errors

    return {
        "name": name,
        "title": str(raw.get("title") or name).strip(),
        "description": str(raw.get("description") or "").strip(),
        "language": str(raw.get("language") or "ru").strip().lower(),
        "fields": fields,
        "zones": zones,
    }, []
