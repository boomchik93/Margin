# -*- coding: utf-8 -*-
"""Оценка уверенности по полям и очередь ручной проверки.

Оценка считается по формату прочитанного значения: дата календарная, в
телефоне хватает цифр, число в заданных границах. Это проверка
правдоподобия, а не правильности чтения — неверно прочитанная цифра даёт
такой же «хороший» телефон. Насколько уверенно модель прочитала значение,
показывает `logprob_min` (см. logprobs.py), и он отдаётся рядом отдельным
блоком.

Совпадение со словарём оценку намеренно не поднимает: иначе исправленные
значения получали бы лучшую оценку из всех.
"""
import re

from schema import CHECKED, UNCHECKED

# Формат поля проверен и соблюдён.
FORMAT_OK = 70
# Свободный текст: проверить нечем.
FORMAT_NONE = 55
# Формат нарушен. Значение почти наверняка прочитано неверно.
FORMAT_BAD = 25

EMPTY = 0

# Ниже этого порога поле уходит на ручную проверку.
REVIEW_THRESHOLD = 50

_RE_DATE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})$")
_RE_NUMBER = re.compile(r"^[+-]?\d+([.,]\d+)?$")

# Четыре одинаковых символа подряд ("МОСКВАААА", "88888888") в свободном
# тексте — признак того, что модель зациклилась.
_RE_REPEAT = re.compile(r"(.)\1\1\1")
_RE_LATIN = re.compile(r"[A-Za-z]")
_RE_CYRILLIC = re.compile(r"[А-Яа-яЁё]")


def _checkDate(field, v):
    m = _RE_DATE.match(v)
    if not m:
        return False
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= d <= 31) or not (1 <= mo <= 12):
        return False
    if field.get("min_year") is not None and y < field["min_year"]:
        return False
    if field.get("max_year") is not None and y > field["max_year"]:
        return False
    return True


def _checkPhone(field, v):
    digits = re.sub(r"\D", "", v)
    if field.get("digits"):
        return len(digits) == field["digits"]
    # E.164 допускает до 15 цифр; короче семи номеров не бывает.
    return 7 <= len(digits) <= 15


def _checkNumber(field, v):
    if not _RE_NUMBER.match(v):
        return False
    val = float(v.replace(",", "."))
    if field.get("min") is not None and val < field["min"]:
        return False
    if field.get("max") is not None and val > field["max"]:
        return False
    return True


def _checkText(field, v, language):
    """True/False, если есть чем проверять; None — проверить нечем."""
    pattern = field.get("format")
    if pattern:
        return re.fullmatch(pattern, v) is not None
    if _RE_REPEAT.search(v):
        return False
    if language in ("ru", "rus", "russian", "русский"):
        # Слово, где смешаны латиница и кириллица, — сорвавшееся чтение:
        # нормализация уже сняла гомоглифы, и остались буквы, которых в
        # русском слове быть не может.
        for word in re.findall(r"[^\W\d_]+", v):
            if _RE_LATIN.search(word) and _RE_CYRILLIC.search(word):
                return False
    return None


def fieldConfidence(field, value, language="ru"):
    """Уверенность по одному полю, 0..100, и причина оценки."""
    if field["type"] == "checkbox":
        if value in (CHECKED, UNCHECKED):
            return FORMAT_OK, "mark"
        return FORMAT_BAD, "unclear"

    text = str(value or "").strip()
    if not text:
        return EMPTY, "empty"

    ftype = field["type"]
    if ftype == "date":
        ok = _checkDate(field, text)
    elif ftype == "phone":
        ok = _checkPhone(field, text)
    elif ftype == "number":
        ok = _checkNumber(field, text)
    else:
        ok = _checkText(field, text, language)

    if ok is None:
        return FORMAT_NONE, "unverified"
    return (FORMAT_OK, "format_ok") if ok else (FORMAT_BAD, "format_bad")


def scoreResult(sch, data):
    """Уверенность по всем полям: (оценки, причины).

    `data` — значения после нормализации, но ДО сверки со словарём:
    оценивается то, что прочитала модель, а не подстановка.
    """
    language = sch.get("language", "ru")
    scores, reasons = {}, {}
    for field in sch["fields"]:
        key = field["key"]
        scores[key], reasons[key] = fieldConfidence(
            field, data.get(key), language)
    return scores, reasons


def reviewQueue(sch, data, scores, dict_status=None):
    """Поля, которые стоит проверить руками, худшие первыми.

    В очередь попадает: нарушенный формат, значение, которого нет в словаре,
    неразличимая отметка и пустое обязательное поле.
    """
    dict_status = dict_status or {}
    queue = []
    for field in sch["fields"]:
        key = field["key"]
        score = scores.get(key, 0)
        if 0 < score < REVIEW_THRESHOLD:
            queue.append((score, key))
        elif dict_status.get(key) == "review":
            queue.append((REVIEW_THRESHOLD, key))
        elif field.get("required") and not str(data.get(key) or "").strip():
            queue.append((0, key))
    return [key for _, key in sorted(queue, key=lambda item: item[0])]
