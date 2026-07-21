# -*- coding: utf-8 -*-
"""Сверка прочитанных значений со словарями допустимых значений.

Словарь — JSON-файл в data/dictionaries/: список строк либо объект с ключом
`values`. Поле схемы ссылается на словарь по имени файла без расширения:
`"dictionary": "cities"` -> data/dictionaries/cities.json.

Что делает сверка:
  - точное совпадение (без учёта регистра, ё/е и лишних пробелов) приводит
    значение к написанию из словаря;
  - расхождение в одну-две похожие буквы считается ошибкой чтения и
    исправляется — но только если для словаря или поля задан порог max_dist;
  - чего в словаре нет, уходит на ручную проверку с ближайшими кандидатами,
    а не подменяется.
"""
import hashlib
import json
import os
import re
import threading
import time

from logging_setup import getLogger

log = getLogger("dict")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Директория словарей. Переопределяется DICT_DIR — в Docker она монтируется
# томом, и обновить словарь можно без пересборки образа.
DICT_DIR = os.environ.get("DICT_DIR") or os.path.join(
    PROJECT_ROOT, "data", "dictionaries")

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# На коротком значении допускаем не больше одной правки: у слова из четырёх
# букв на расстоянии двух правок лежит полсловаря.
SHORT_LEN = 4
# Короче этого подбирать по похожести нельзя вовсе: ближайший кандидат на
# такой длине случаен. Только точное совпадение.
EXACT_ONLY_LEN = 4

# Загруженные словари: имя -> список значений. Объекты обновляются на месте
# (clear + update), а не переприсваиванием: другие модули держат ссылки
# именно на них.
DB = {}
# Порог подстановки из самого файла словаря: имя -> max_dist.
THRESHOLDS = {}
LOAD_ERRORS = []
LOAD_REPORT = {}

_lock = threading.Lock()
_fingerprint = None
_last_check = 0.0

# Подхват изменений директории во время работы. На замерах точности
# пересборка словарей посреди партии сдвигает результат — там отключают.
AUTO_RELOAD = (os.environ.get("DICT_AUTO_RELOAD", "1").strip().lower()
               not in ("0", "false", "no", "off"))
AUTO_RELOAD_SEC = float(os.environ.get("DICT_AUTO_RELOAD_SEC", "0") or 0)


# === ЗАГРУЗКА ===

def _files():
    try:
        return sorted(n for n in os.listdir(DICT_DIR)
                      if n.lower().endswith(".json") and not n.startswith("."))
    except OSError:
        return []


def fingerprint():
    """Отпечаток директории: имена, размеры и время правки файлов."""
    out = []
    for n in _files():
        try:
            st = os.stat(os.path.join(DICT_DIR, n))
            out.append((n, st.st_size, int(st.st_mtime * 1000)))
        except OSError:
            continue
    return tuple(out)


def _values(payload, filename, errors):
    """Список значений из содержимого файла. None, если формат не тот."""
    if isinstance(payload, dict):
        payload = payload.get("values")
    if not isinstance(payload, list):
        errors.append("%s: ожидается список значений или объект с ключом "
                      "values" % filename)
        return None
    values, skipped = [], 0
    seen = set()
    for item in payload:
        if not isinstance(item, (str, int, float)) or isinstance(item, bool):
            skipped += 1
            continue
        text = re.sub(r"\s+", " ", str(item)).strip()
        if not text or norm(text) in seen:
            skipped += 1
            continue
        seen.add(norm(text))
        values.append(text)
    return values, skipped


def reload():
    """Перечитать словари с диска. Возвращает состояние после загрузки."""
    global _fingerprint
    with _lock:
        db, thresholds, errors, report = {}, {}, [], {}
        for filename in _files():
            name = os.path.splitext(filename)[0]
            if not NAME_RE.match(name):
                errors.append("%s: имя файла — латиница, цифры, дефис и "
                              "подчёркивание" % filename)
                continue
            try:
                with open(os.path.join(DICT_DIR, filename),
                          encoding="utf-8") as f:
                    payload = json.load(f)
            except (OSError, ValueError) as e:
                # Словари правят руками, и лишняя запятая не должна
                # останавливать распознавание: битый файл пропускается, а
                # остальные работают.
                errors.append("%s: не разобран как JSON (%s)" % (filename, e))
                continue

            got = _values(payload, filename, errors)
            if got is None:
                continue
            values, skipped = got
            db[name] = values
            report[name] = {"file": filename, "values": len(values),
                            "skipped": skipped}

            if isinstance(payload, dict) and payload.get("max_dist") is not None:
                try:
                    limit = float(payload["max_dist"])
                    if limit <= 0:
                        raise ValueError("не больше нуля")
                    thresholds[name] = limit
                    report[name]["max_dist"] = limit
                except (TypeError, ValueError):
                    errors.append("%s: max_dist должен быть положительным "
                                  "числом" % filename)

        DB.clear()
        DB.update(db)
        THRESHOLDS.clear()
        THRESHOLDS.update(thresholds)
        LOAD_ERRORS[:] = errors
        LOAD_REPORT.clear()
        LOAD_REPORT.update(report)
        _fingerprint = fingerprint()

    for message in errors:
        log.warning("словарь не загружен", extra={"reason": message})
    log.info("словари загружены", extra={
        "dir": DICT_DIR,
        "dictionaries": {k: len(v) for k, v in db.items()},
        "version": dictVersion()})
    return status()


def ensureFresh(force=False):
    """Перечитать словари, если директория изменилась. True, если перечитал."""
    global _last_check
    if not force:
        if not AUTO_RELOAD:
            return False
        now = time.time()
        if AUTO_RELOAD_SEC and now - _last_check < AUTO_RELOAD_SEC:
            return False
        _last_check = now
        if fingerprint() == _fingerprint:
            return False
    reload()
    return True


def dbLoaded():
    """Есть ли хотя бы один непустой словарь."""
    return any(DB[k] for k in DB)


def dictVersion():
    """Короткий отпечаток содержимого словарей.

    Пишется в историю рядом с результатом: по нему видно, с какой версией
    словарей сверялась страница.
    """
    if not DB:
        return ""
    h = hashlib.sha1()
    for name in sorted(DB):
        h.update(name.encode("utf-8"))
        for value in DB[name]:
            h.update(b"\0" + value.encode("utf-8"))
    return h.hexdigest()[:12]


def status():
    """Состояние словарей для GET /api/dictionaries."""
    return {
        "dir": DICT_DIR,
        "dir_exists": os.path.isdir(DICT_DIR),
        "loaded": dbLoaded(),
        "version": dictVersion(),
        "dictionaries": {name: dict(info) for name, info in LOAD_REPORT.items()},
        "auto_reload": {"enabled": AUTO_RELOAD,
                        "min_interval_sec": AUTO_RELOAD_SEC},
        "errors": list(LOAD_ERRORS),
    }


# === РАССТОЯНИЕ ===

# Пары, которые путаются в рукописи: расхождение только по ним почти
# наверняка ошибка чтения глифа, поэтому замена стоит дешевле.
CONFUSABLE = [
    ("Б", "Ь"), ("Б", "Г"), ("Б", "В"), ("Й", "Б"), ("Г", "Р"), ("Г", "Т"),
    ("И", "Ч"), ("И", "Н"), ("П", "Л"), ("П", "Н"), ("А", "Л"), ("О", "С"),
    ("О", "Ф"), ("Е", "Ё"), ("Ш", "Щ"), ("Ц", "Щ"), ("З", "Э"), ("Я", "Э"),
    ("М", "Н"), ("У", "Ч"), ("Х", "Ж"), ("Д", "А"), ("К", "Ж"), ("Ъ", "Ь"),
    # латиница, подставленная моделью вместо кириллицы
    ("P", "Р"), ("C", "С"), ("O", "О"), ("A", "А"), ("B", "В"), ("E", "Е"),
    ("H", "Н"), ("K", "К"), ("M", "М"), ("T", "Т"), ("X", "Х"), ("Y", "У"),
    # буква вместо цифры и наоборот
    ("0", "О"), ("3", "З"), ("6", "Б"), ("4", "Ч"),
    # цифры, похожие начертанием
    ("1", "7"), ("4", "9"), ("3", "8"), ("5", "6"), ("0", "6"),
    # латинские рукописные
    ("I", "L"), ("U", "V"), ("M", "N"), ("O", "Q"),
]
_CONF = set()
for _x, _y in CONFUSABLE:
    _CONF.add((_x, _y))
    _CONF.add((_y, _x))


def norm(s):
    """Ключ сравнения: регистр, ё/е и служебные пробелы значения не несут."""
    return re.sub(r"\s+", " ", str(s or "")).strip().upper().replace("Ё", "Е")


def confusableDist(a, b):
    """Расстояние Левенштейна с половинной ценой замены в паре похожих знаков."""
    la, lb = len(a), len(b)
    if not la:
        return float(lb)
    if not lb:
        return float(la)
    prev = [float(j) for j in range(lb + 1)]
    for i in range(1, la + 1):
        cur = [float(i)] + [0.0] * lb
        for j in range(1, lb + 1):
            ca, cb = a[i - 1], b[j - 1]
            if ca == cb:
                cost = 0.0
            elif (ca, cb) in _CONF:
                cost = 0.5
            else:
                cost = 1.0
            cur[j] = min(prev[j] + 1.0, cur[j - 1] + 1.0, prev[j - 1] + cost)
        prev = cur
    return prev[lb]


# === ПОДБОР ===

def matchValue(value, candidates, max_dist=None):
    """Подобрать значение из словаря. Возвращает (значение, статус).

    Статусы:
      exact  — найдено точно, значение приведено к написанию из словаря;
      fixed  — исправлено на единственного ближайшего кандидата;
      review — в словаре нет, значение оставлено как прочитано;
      skip   — сверять нечего: значение пустое или словарь пуст.

    `max_dist` — наибольшее расстояние, на котором расхождение ещё считается
    ошибкой чтения. None означает «не подставлять»: порог для каждого словаря
    набирается замером, и подстановка «на глаз» чинила бы вслепую.
    """
    raw = str(value or "").strip()
    if not raw or not candidates:
        return raw, "skip"

    key = norm(raw)
    index = {}
    for c in candidates:
        index.setdefault(norm(c), c)
    if key in index:
        return index[key], "exact"

    if max_dist is None or len(key) < EXACT_ONLY_LEN:
        return raw, "review"

    limit = min(1.0, max_dist) if len(key) <= SHORT_LEN else max_dist
    best, best_d, ties = None, None, 0
    for nk, orig in index.items():
        # длина отличается больше порога — считать расстояние незачем
        if abs(len(nk) - len(key)) > limit:
            continue
        d = confusableDist(key, nk)
        if d > limit:
            continue
        if best_d is None or d < best_d - 1e-9:
            best, best_d, ties = orig, d, 1
        elif abs(d - best_d) < 1e-9 and orig != best:
            ties += 1
    # Два кандидата на равном расстоянии — выбор между ними случаен, и
    # подставлять любой из них нельзя.
    if best is not None and ties == 1:
        return best, "fixed"
    return raw, "review"


def nearestCandidates(name, value, limit=2):
    """Ближайшие значения словаря: [{"value": ..., "distance": ...}].

    Два кандидата, а не один: равное расстояние у обоих значит, что выбор
    между ними случаен, и по паре это видно.
    """
    raw = str(value or "").strip()
    candidates = DB.get(name) or []
    if not raw or not candidates:
        return []
    key = norm(raw)
    scored = []
    for c in candidates:
        nk = norm(c)
        # грубая отсечка по длине, чтобы не считать расстояние до всего словаря
        if abs(len(nk) - len(key)) > max(3, len(key) // 2):
            continue
        scored.append((confusableDist(key, nk), c))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [{"value": c, "distance": round(d, 2)} for d, c in scored[:limit]]


def applyDictionaries(sch, data):
    """Сверяет поля схемы со словарями.

    Возвращает (данные, статусы, кандидаты). Статус есть только у полей, для
    которых в схеме указан словарь; кандидаты — только у ушедших на проверку.
    """
    out = dict(data)
    statuses, candidates = {}, {}
    for field in sch["fields"]:
        name = field.get("dictionary")
        if not name:
            continue
        key = field["key"]
        if name not in DB:
            # Словарь указан в схеме, но файла нет. Это не «значение не
            # найдено», а сбой настройки, и выглядеть он должен иначе.
            statuses[key] = "no_dictionary"
            continue

        before = out.get(key, "")
        max_dist = field.get("max_dist", THRESHOLDS.get(name))
        after, status = matchValue(before, DB[name], max_dist=max_dist)
        statuses[key] = status
        out[key] = after

        if status == "review":
            candidates[key] = nearestCandidates(name, before)
        if status in ("fixed", "review"):
            # Каждое решение словаря идёт в журнал: итог не совпадает с тем,
            # что прочитала модель, и без записи выглядит взявшимся ниоткуда.
            log.info("решение словаря", extra={
                "field": key, "dictionary": name, "recognized": before,
                "result": after, "status": status, "max_dist": max_dist,
                "dictionary_size": len(DB[name]),
                "candidates": candidates.get(key)})
    return out, statuses, candidates


# Инициализация в конце модуля: загрузка пользуется norm(), определённым выше.
try:
    reload()
except Exception as e:  # словари не обязаны существовать
    log.warning("словари не загружены при старте", extra={"error": str(e)})
