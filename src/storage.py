# -*- coding: utf-8 -*-
"""История распознаваний в SQLite.

Ответ сервиса живёт только в HTTP-соединении. Без хранилища результат
теряется при обрыве связи, а на вопрос «что сервис вернул по этому файлу
месяц назад» ответить можно только пересчётом.

SQLite, а не отдельная СУБД: хранилище должно подниматься само, без
администратора. Объёмы маленькие, запись однопоточная, чтения редкие. При
переезде на сетевую БД меняется только этот модуль.
"""

import contextlib
import json
import os
import sqlite3
import threading
import time

from logging_setup import getLogger

log = getLogger("storage")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Путь к файлу БД. Переопределяется RESULTS_DB — в Docker он лежит на
# смонтированном томе, иначе история пропадёт с контейнером.
DB_PATH = os.environ.get("RESULTS_DB") or os.path.join(
    PROJECT_ROOT, "data", "results.db")

# Хранение можно выключить целиком: на замере точности история только мешает,
# а там, где распознанный текст хранить нельзя, её и не должно быть.
ENABLED = (os.environ.get("RESULTS_DB_ENABLED") or "1").strip().lower() not in (
    "0", "false", "no", "off")

# Схема узкая: одна строка на страницу. Поля документа лежат JSON-ом, в
# колонки вынесено только то, по чему ищут. Разносить поля по колонкам
# нельзя: их состав задаётся схемой документа и у каждой схемы свой.
SCHEMA = """
CREATE TABLE IF NOT EXISTS recognitions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id        TEXT    NOT NULL,
    created_at        TEXT    NOT NULL,
    source_name       TEXT,
    source_type       TEXT,
    page              INTEGER,
    pages_total       INTEGER,
    status            TEXT    NOT NULL,
    error             TEXT,
    duration_seconds  REAL,
    mode              TEXT,
    schema_name       TEXT,
    text              TEXT,
    fields_json       TEXT,
    confidence_json   TEXT,
    dict_status_json  TEXT,
    trace_json        TEXT,
    needs_review_json TEXT,
    review_count      INTEGER DEFAULT 0,
    min_confidence    INTEGER,
    dict_version      TEXT,
    model_name        TEXT
);

CREATE INDEX IF NOT EXISTS idx_recognitions_request ON recognitions(request_id);
CREATE INDEX IF NOT EXISTS idx_recognitions_created ON recognitions(created_at);
CREATE INDEX IF NOT EXISTS idx_recognitions_schema  ON recognitions(schema_name);
CREATE INDEX IF NOT EXISTS idx_recognitions_review  ON recognitions(review_count);
"""

# Одна запись за раз. Flask обслуживает запросы в потоках, а SQLite-соединение
# не переносится между ними; блокировка проще пула на таких объёмах.
_lock = threading.Lock()
_init_error = ""
_ready = False


@contextlib.contextmanager
def _connect():
    """Соединение с БД на время одной операции.

    Закрывается явно: `with sqlite3.connect(...)` управляет транзакцией, но
    не закрывает соединение, и на сервисе, где оно открывается на каждую
    страницу, это утечка дескрипторов.
    """
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn.row_factory = sqlite3.Row
        # WAL: чтение истории не блокирует запись очередной страницы.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init():
    """Создать файл и схему. Идемпотентна, вызывается при старте."""
    global _ready, _init_error
    if not ENABLED:
        log.info("хранение результатов отключено",
                 extra={"reason": "RESULTS_DB_ENABLED=0"})
        return False
    try:
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        with _lock, _connect() as conn:
            conn.executescript(SCHEMA)
        _ready = True
        _init_error = ""
        log.info("хранилище результатов готово", extra={"db_path": DB_PATH})
        return True
    except (sqlite3.Error, OSError) as e:
        _ready = False
        _init_error = str(e)
        # Не поднимаем исключение: сервис обязан работать и без истории.
        log.warning("хранилище результатов недоступно, история не пишется",
                    extra={"db_path": DB_PATH, "error": str(e)})
        return False


def ready():
    return _ready


def _minConfidence(scores):
    """Минимальная уверенность по странице, пустые поля не считаем."""
    values = [int(v) for v in (scores or {}).values()
              if isinstance(v, (int, float)) and v > 0]
    return min(values) if values else None


def save(record):
    """Сохранить результат одной страницы. Возвращает id строки или None."""
    if not ENABLED or not _ready:
        return None

    fields = record.get("fields") or {}
    confidence = record.get("confidence") or {}
    needs_review = record.get("needs_review") or []

    row = (
        record.get("request_id") or "",
        record.get("created_at") or time.strftime("%Y-%m-%dT%H:%M:%S"),
        record.get("source_name") or "",
        record.get("source_type") or "",
        record.get("page"),
        record.get("pages_total"),
        record.get("status") or "ok",
        record.get("error") or "",
        record.get("duration_seconds"),
        record.get("mode") or "",
        record.get("schema_name") or "",
        record.get("text") or "",
        json.dumps(fields, ensure_ascii=False),
        json.dumps(confidence, ensure_ascii=False),
        json.dumps(record.get("dict_status") or {}, ensure_ascii=False),
        json.dumps(record.get("trace") or {}, ensure_ascii=False),
        json.dumps(needs_review, ensure_ascii=False),
        len(needs_review),
        _minConfidence(confidence),
        record.get("dict_version") or "",
        record.get("model_name") or "",
    )

    sql = """INSERT INTO recognitions (
        request_id, created_at, source_name, source_type, page, pages_total,
        status, error, duration_seconds, mode, schema_name, text, fields_json,
        confidence_json, dict_status_json, trace_json, needs_review_json,
        review_count, min_confidence, dict_version, model_name
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""

    try:
        with _lock, _connect() as conn:
            cur = conn.execute(sql, row)
            row_id = cur.lastrowid
        log.debug("результат сохранён", extra={"row_id": row_id,
                                               "page": record.get("page")})
        return row_id
    except sqlite3.Error as e:
        log.warning("не удалось сохранить результат",
                    extra={"error": str(e), "page": record.get("page")})
        return None
