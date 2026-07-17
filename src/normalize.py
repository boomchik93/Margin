# -*- coding: utf-8 -*-
"""Нормализация прочитанных значений по типу поля.

Принцип: приводить формат, но никогда не выдумывать данные. Ни одно поле не
заполняется значением по умолчанию, и ни одно значение не «чинится» до
правдоподобного: невозможная дата остаётся как прочитана и уходит на ручную
проверку, а не превращается в возможную.
"""
import datetime
import re

from schema import toCheckbox

# Тот же глиф в другой кодовой точке: латинская "P" в русском слове на вид
# неотличима от кириллической "Р", но поиск и сверка со словарём такое слово
# не находят. Только латиница -> кириллица и только в словах, где кириллица
# уже есть: обратная замена испортила бы латинские коды и обозначения.
HOMOGLYPHS = {
    "P": "Р", "C": "С", "O": "О", "A": "А", "B": "В", "E": "Е",
    "H": "Н", "K": "К", "M": "М", "T": "Т", "X": "Х", "Y": "У",
    "p": "р", "c": "с", "o": "о", "a": "а", "e": "е", "x": "х", "y": "у",
}

_CYRILLIC = re.compile(r"[А-Яа-яЁё]")


def fixHomoglyphs(text):
    """Латинские глифы внутри кириллических слов -> кириллические."""
    s = str(text or "")
    if not s or not _CYRILLIC.search(s):
        return s

    return "".join(HOMOGLYPHS.get(ch, ch) for ch in s)


def _text(field, value, language):
    s = re.sub(r"\s+", " ", str(value or "")).strip()
    if language in ("ru", "rus", "russian", "русский"):
        s = fixHomoglyphs(s)
    case = field.get("case", "keep")
    if case == "upper":
        s = s.upper()
    elif case == "lower":
        s = s.lower()
    return s


def _number(value):
    # Пробелы внутри числа — оформление, а не значение. Запятую на точку не
    # меняем: это уже решение принимающей стороны, а не нормализация.
    return re.sub(r"\s+", "", str(value or ""))


def _phone(value):
    s = str(value or "").strip()
    digits = re.sub(r"\D", "", s)
    if not digits:
        return ""
    return ("+" if s.startswith("+") else "") + digits


_DATE_RE = re.compile(r"^(\d{1,2})[.\s/\-](\d{1,2})[.\s/\-](\d{2,4})$")


def normalizeDate(value, today=None):
    """ДД.ММ.ГГГГ из разных разделителей; неполную дату оставляем как есть."""
    s = str(value or "").strip()
    if not s:
        return ""
    m = _DATE_RE.match(s)
    if not m:
        return s
    d, mo, y = m.groups()
    if len(y) == 3:
        return s
    if len(y) == 2:
        # Двузначный год: век выбираем так, чтобы дата не ушла в будущее
        # дальше десяти лет. "85" -> 1985, "24" -> 2024.
        now = (today or datetime.date.today()).year
        full = 2000 + int(y)
        if full > now + 10:
            full -= 100
        y = str(full)
    return "%s.%s.%s" % (d.zfill(2), mo.zfill(2), y)
