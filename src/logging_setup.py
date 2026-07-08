# -*- coding: utf-8 -*-
"""Настройка логирования: JSON в файл, текст в консоль."""

import contextvars
import datetime
import json
import logging
import logging.handlers
import os
import sys
import time
import traceback
import uuid

# Корень проекта: src/ лежит на уровень ниже.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Директория логов. Переопределяется LOG_DIR — в Docker она монтируется
# наружу, иначе логи умрут вместе с контейнером.
LOG_DIR = os.environ.get("LOG_DIR") or os.path.join(PROJECT_ROOT, "logs")

# Сырой вывод модели по каждому проходу. Он объёмный (десятки килобайт на
# зону) и нужен редко, поэтому пишется только по явному включению.
LOG_RAW = os.environ.get("LOG_RAW_MODEL_OUTPUT", "").strip().lower() in (
    "1", "true", "yes", "on")
RAW_DIR = os.path.join(LOG_DIR, "raw")

# Размер файла до ротации и число хранимых копий.
MAX_BYTES = int(os.environ.get("LOG_MAX_BYTES") or 50 * 1024 * 1024)
BACKUP_COUNT = int(os.environ.get("LOG_BACKUP_COUNT") or 10)

# файл подробнее консоли: в файл всё для разбора, в консоль — за чем следить
FILE_LEVEL = (os.environ.get("LOG_LEVEL") or "DEBUG").upper()
CONSOLE_LEVEL = (os.environ.get("LOG_CONSOLE_LEVEL") or "INFO").upper()

# Ключи LogRecord, которые есть у любой записи. Всё, что сверх этого набора,
# положено вызывающим через extra= и уходит в JSON как есть.
_STANDARD = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "message", "asctime",
}

# Идентификатор запроса, видимый всем записям в пределах обработки одного
# HTTP-запроса.
_request_id = contextvars.ContextVar("request_id", default="")

_configured = False


def newRequestId():
    """Короткий идентификатор запроса, восьми знаков хватает."""
    return uuid.uuid4().hex[:8]


def setRequestId(value):
    """Привязать идентификатор к текущему контексту."""
    _request_id.set(value or "")
    return value


def getRequestId():
    return _request_id.get()


class _JsonFormatter(logging.Formatter):
    """Одна запись — один JSON-объект в строке."""

    def format(self, record):
        entry = {
            "ts": datetime.datetime.fromtimestamp(
                record.created, datetime.timezone.utc
            ).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }

        rid = getattr(record, "request_id", "") or getRequestId()
        if rid:
            entry["request_id"] = rid

        for key, value in record.__dict__.items():
            if key in _STANDARD or key == "request_id":
                continue
            try:
                json.dumps(value, ensure_ascii=False)
                entry[key] = value
            except (TypeError, ValueError):
                entry[key] = repr(value)

        if record.exc_info:
            entry["exception"] = "".join(
                traceback.format_exception(*record.exc_info)).strip()

        entry.setdefault("source", f"{record.module}:{record.lineno}")

        return json.dumps(entry, ensure_ascii=False, default=str)


class _ConsoleFormatter(logging.Formatter):
    """Человекочитаемая строка для консоли и errors.log."""

    def __init__(self):
        super().__init__(
            fmt="%(asctime)s %(levelname)-7s %(name)-14s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S")

    def format(self, record):
        base = super().format(record)

        rid = getattr(record, "request_id", "") or getRequestId()
        if rid:
            base = base.replace(record.getMessage(),
                                f"[{rid}] {record.getMessage()}", 1)

        extras = []
        for key, value in record.__dict__.items():
            if key in _STANDARD or key == "request_id":
                continue
            text = str(value)
            # Длинные значения (сырой ответ модели, список кандидатов) в
            # консоли обрезаем: целиком они есть в app.log.
            if len(text) > 200:
                text = text[:200] + "…"
            extras.append(f"{key}={text}")
        if extras:
            base = f"{base}  {' '.join(extras)}"

        if record.exc_info:
            base = f"{base}\n{''.join(traceback.format_exception(*record.exc_info)).rstrip()}"
        return base


class _RequestIdFilter(logging.Filter):
    """Проставляет request_id записям, сделанным без явного extra."""

    def filter(self, record):
        if not getattr(record, "request_id", ""):
            record.request_id = getRequestId()
        return True


class _SafeLogger(logging.Logger):
    """Логгер, у которого extra не может уронить запись."""

    def makeRecord(self, name, level, fn, lno, msg, args, exc_info,
                   func=None, extra=None, sinfo=None):
        if extra:
            safe = {}
            for key, value in extra.items():
                # request_id обрабатывается отдельно фильтром и разрешён.
                if key in _STANDARD and key != "request_id":
                    safe[key + "_"] = value
                else:
                    safe[key] = value
            extra = safe
        return super().makeRecord(name, level, fn, lno, msg, args, exc_info,
                                  func, extra, sinfo)


# Класс логгера ставится до создания логгеров модулями: logging.getLogger()
# кэширует объекты, и смена класса задним числом на уже созданные не влияет.
logging.setLoggerClass(_SafeLogger)


def setup(force=False):
    """Настроить логирование процесса. Повторные вызовы игнорируются."""
    global _configured
    if _configured and not force:
        return logging.getLogger("ocr")

    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        if LOG_RAW:
            os.makedirs(RAW_DIR, exist_ok=True)
        writable = os.access(LOG_DIR, os.W_OK)
    except OSError:
        writable = False

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    id_filter = _RequestIdFilter()

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(getattr(logging, CONSOLE_LEVEL, logging.INFO))
    console.setFormatter(_ConsoleFormatter())
    console.addFilter(id_filter)
    root.addHandler(console)

    if writable:
        app_file = logging.handlers.RotatingFileHandler(
            os.path.join(LOG_DIR, "app.log"),
            maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8")
        app_file.setLevel(getattr(logging, FILE_LEVEL, logging.DEBUG))
        app_file.setFormatter(_JsonFormatter())
        app_file.addFilter(id_filter)
        root.addHandler(app_file)

        err_file = logging.handlers.RotatingFileHandler(
            os.path.join(LOG_DIR, "errors.log"),
            maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8")
        err_file.setLevel(logging.WARNING)
        err_file.setFormatter(_ConsoleFormatter())
        err_file.addFilter(id_filter)
        root.addHandler(err_file)
    else:
        # только консоль: штатно при неверных правах на том, но молчать нельзя
        logging.getLogger("ocr").warning(
            "директория логов недоступна для записи, файловые логи отключены",
            extra={"log_dir": LOG_DIR})

    # Flask/werkzeug пишет свой access-лог; он дублирует наш и в JSON не нужен.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    _configured = True

    log = logging.getLogger("ocr")
    log.info("логирование настроено", extra={
        "log_dir": LOG_DIR,
        "file_level": FILE_LEVEL,
        "console_level": CONSOLE_LEVEL,
        "raw_model_output": LOG_RAW,
        "rotation_mb": round(MAX_BYTES / 1024 / 1024, 1),
        "backups": BACKUP_COUNT,
        "files_enabled": writable,
    })
    return log


def getLogger(name):
    """Логгер модуля, все имена в пространстве `ocr.*`."""
    return logging.getLogger(name if name.startswith("ocr") else f"ocr.{name}")


# --- аудит распознаваний ---------------------------------------------------
# отдельный файл: у аудита свой срок хранения. Формат JSON Lines

_audit_logger = None


def _auditLogger():
    global _audit_logger
    if _audit_logger is not None:
        return _audit_logger

    logger = logging.getLogger("ocr.audit")
    logger.setLevel(logging.INFO)
    # Аудит не должен попадать в app.log: он большой и повторяет то, что там
    # уже разложено по событиям.
    logger.propagate = False

    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            os.path.join(LOG_DIR, "audit.jsonl"),
            maxBytes=MAX_BYTES,
            # Аудит хранится дольше отладки: по нему разбирают спорные случаи
            # спустя месяцы.
            backupCount=int(os.environ.get("AUDIT_BACKUP_COUNT") or 50),
            encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    except OSError as e:
        logging.getLogger("ocr").warning(
            "аудит-лог недоступен", extra={"error": str(e)})
        logger.addHandler(logging.NullHandler())

    _audit_logger = logger
    return logger


def audit(record):
    """Записать в аудит одну распознанную страницу."""
    payload = dict(record or {})
    payload.setdefault("ts", datetime.datetime.now(
        datetime.timezone.utc).isoformat(timespec="milliseconds"))
    payload.setdefault("request_id", getRequestId())
    try:
        line = json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError) as e:
        line = json.dumps({
            "ts": payload["ts"],
            "request_id": payload.get("request_id", ""),
            "audit_error": f"запись не сериализуется: {e}",
        }, ensure_ascii=False)
    _auditLogger().info(line)


def saveRawOutput(tag, text):
    """Сохранить сырой вывод модели, если включён LOG_RAW_MODEL_OUTPUT."""
    if not LOG_RAW or not text:
        return ""
    rid = getRequestId() or "norequest"
    name = f"{rid}_{tag}_{int(time.time() * 1000)}.txt"
    path = os.path.join(RAW_DIR, name)
    try:
        os.makedirs(RAW_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path
    except OSError:
        return ""


class stage:
    """Замер стадии пайплайна: время и исход."""

    def __init__(self, logger, name, **fields):
        self.logger = logger
        self.name = name
        self.fields = dict(fields)
        self.started = 0.0

    def add(self, **fields):
        """Дописать поля, известные только по завершении стадии."""
        self.fields.update(fields)
        return self

    def __enter__(self):
        self.started = time.time()
        self.logger.debug(f"стадия начата: {self.name}",
                          extra={"stage": self.name, **self.fields})
        return self

    def __exit__(self, exc_type, exc, tb):
        elapsed = round(time.time() - self.started, 3)
        if exc_type is not None:
            self.logger.error(
                f"стадия провалена: {self.name}",
                extra={"stage": self.name, "seconds": elapsed,
                       "error": str(exc), "error_type": exc_type.__name__,
                       **self.fields},
                exc_info=(exc_type, exc, tb))
            return False
        self.logger.info(f"стадия завершена: {self.name}",
                         extra={"stage": self.name, "seconds": elapsed,
                                **self.fields})
        return False
