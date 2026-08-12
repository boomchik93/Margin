#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""HTTP-сервис распознавания рукописного текста.

Тут только транспорт: приём файла, вызов пайплайна, форма ответа.
Распознавание в ocr.py, схемы полей в schema.py, словари в dictmatch.py.
"""

import atexit
import json
import os
import sys
import time
import uuid

# нужно для запуска не из src (gunicorn, тесты)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import logging_setup

# до импорта пайплайна: ocr и dictmatch пишут в лог уже на импорте
logging_setup.setup()
log = logging_setup.getLogger("api")

from flask import Flask, request, jsonify, render_template, g
from flask_cors import CORS
from flasgger import Swagger
from werkzeug.exceptions import HTTPException
from werkzeug.utils import secure_filename

import dictmatch
import schema as schemas
import storage
from ocr import Engine, HAS_FITZ, extractImagesFromPdf, loadConfig

config = loadConfig()
engine = Engine(config)
atexit.register(engine.stop)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = config["server"]["max_content_length"]
app.config["UPLOAD_FOLDER"] = config["paths"]["upload_folder"]
app.json.ensure_ascii = False

if config["api"]["enable_cors"]:
    CORS(app)

# --- Swagger ---
# UI на /apidocs, сырая спека на /apispec.json.
SWAGGER_TEMPLATE = {
    "swagger": "2.0",
    "info": {
        "title": "Распознавание рукописного текста",
        "description":
            "На вход изображение или PDF, на выходе рукописный текст "
            "страницы либо поля документа по схеме.\n\n"
            "**Два режима.** Без параметра `schema` сервис расшифровывает "
            "весь рукописный текст страницы (`mode: text`). С параметром "
            "`schema` извлекает поля, описанные в схеме (`mode: form`): "
            "значение, оценку уверенности, результат сверки со словарём и "
            "список полей на ручную проверку.\n\n"
            "**Идентификатор запроса.** Каждый ответ содержит `request_id` "
            "и заголовок `X-Request-ID`. Если клиент прислал свой "
            "`X-Request-ID`, сервис берёт его (буквы, цифры, `-` и `_`, до "
            "64 символов). По нему в журналах собирается вся история "
            "обработки и ищется запись через `/api/results/{request_id}`.\n\n"
            "**Ошибки.** Любая ошибка отдаётся как JSON с `success: false`, "
            "текстом в `error` и `request_id`. Текст исключения наружу не "
            "уходит: в нём бывают пути и куски распознанных данных.",
        "version": "1.0",
    },
    "consumes": ["multipart/form-data"],
    "produces": ["application/json"],
    "tags": [
        {"name": "Распознавание", "description": "Загрузка файла и разбор"},
        {"name": "Схемы", "description": "Описания полей документов"},
        {"name": "Словари", "description": "Допустимые значения полей"},
        {"name": "История", "description": "Сохранённые результаты"},
        {"name": "Служебные", "description": "Здоровье, статус, конфигурация"},
    ],
    "definitions": {
        "Error": {
            "type": "object",
            "properties": {
                "success": {"type": "boolean", "example": False},
                "error": {"type": "string", "example": "нет файла"},
                "request_id": {"type": "string", "example": "3f2a91c4"},
            },
        },
        "PageResult": {
            "type": "object",
            "description": "Разбор одной страницы.",
            "properties": {
                "page": {"type": "integer",
                         "description": "Номер страницы, только для PDF"},
                "mode": {"type": "string", "enum": ["text", "form"]},
                "parsed": {"type": "boolean",
                           "description": "false — ответ модели не разобран "
                                          "как JSON"},
                "error": {"type": "string",
                          "description": "Причина сбоя чтения страницы"},
                "text": {"type": "string",
                         "description": "mode=text: текст страницы"},
                "lines": {"type": "array", "items": {"type": "string"},
                          "description": "mode=text: строки сверху вниз"},
                "unclear_count": {
                    "type": "integer",
                    "description": "mode=text: сколько слов модель пометила "
                                   "как неразборчивые знаком [?]"},
                "line_logprob_min": {
                    "type": "array", "items": {"type": "number"},
                    "description": "mode=text: уверенность чтения по "
                                   "строкам, только при backend=server"},
                "schema": {"type": "string",
                           "description": "mode=form: имя схемы"},
                "fields": {"type": "object",
                           "description": "mode=form: поля документа, все "
                                          "ключи схемы присутствуют всегда"},
                "confidence": {"type": "object",
                               "description": "mode=form: уверенность по "
                                              "полям, 0..100"},
                "confidence_reason": {"type": "object"},
                "dict_status": {
                    "type": "object",
                    "description": "mode=form: exact / fixed / review / "
                                   "skip / no_dictionary"},
                "candidates": {
                    "type": "object",
                    "description": "mode=form: ближайшие значения словаря "
                                   "для полей со статусом review"},
                "needs_review": {"type": "array", "items": {"type": "string"},
                                 "description": "mode=form: поля на ручную "
                                                "проверку"},
                "logprob_min": {
                    "type": "object",
                    "description": "mode=form: уверенность чтения по полям, "
                                   "только при backend=server"},
                "backend": {"type": "string", "enum": ["cli", "server"]},
                "model": {"type": "string"},
                "timings": {"type": "object"},
            },
        },
    },
}

SWAGGER_CONFIG = {
    "headers": [],
    "specs": [{"endpoint": "apispec", "route": "/apispec.json"}],
    "static_url_path": "/flasgger_static",
    "swagger_ui": True,
    "specs_route": "/apidocs/",
}

swagger = Swagger(app, template=SWAGGER_TEMPLATE, config=SWAGGER_CONFIG)

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "bmp", "webp", "pdf"}

# Писать ли распознанное содержимое в аудит-журнал. Выключается там, где
# текст документов нельзя хранить вне хранилища результатов.
AUDIT_CONTENT = (os.environ.get("AUDIT_LOG_CONTENT") or "1").strip().lower() \
    not in ("0", "false", "no", "off")

# недоступность хранилища не мешает работе, save() дальше молчит
storage.init()

request_counts = {}


def checkRateLimit():
    """Счётчик запросов в памяти процесса, по адресу клиента."""
    limit = config["api"]["rate_limit"]
    if not limit["enabled"]:
        return True

    client = request.remote_addr
    now = time.time()
    recent = [t for t in request_counts.get(client, []) if t > now - 60]
    if len(recent) >= limit["requests_per_minute"]:
        request_counts[client] = recent
        return False
    recent.append(now)
    request_counts[client] = recent
    return True


def allowedFile(filename):
    return ("." in filename
            and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS)


def _flag(name):
    value = request.form.get(name) or request.args.get(name) or ""
    return value.strip().lower() in ("1", "true", "yes", "on")


def resolveSchema():
    """Схема запроса: (схема или None, текст ошибки или None).

    `schema` — имя схемы из config/schemas, `schema_json` — схема прямо в
    запросе, для клиентов, которые описывают свой документ сами.
    """
    inline = request.form.get("schema_json")
    if inline:
        try:
            raw = json.loads(inline)
        except ValueError:
            return None, "schema_json не разобран как JSON"
        sch, errors = schemas.validate(raw, name="inline")
        if errors:
            return None, "схема не прошла проверку: " + "; ".join(errors)
        return sch, None

    name = (request.form.get("schema") or request.args.get("schema")
            or "").strip()
    if not name:
        return None, None
    sch = schemas.get(name)
    if sch is None:
        return None, "схема не найдена: %s" % name
    return sch, None


def pageEntry(result, page=None):
    """Секция ответа по одной странице: трассировка наружу не идёт."""
    entry = {k: v for k, v in result.items()
             if k not in ("trace", "dict_version")}
    if page is not None:
        entry = {"page": page, **entry}
    return entry


def recordPage(result, source_name, source_type, page, pages_total, duration):
    """Пишет страницу в аудит-журнал и в историю."""
    is_form = result.get("mode") == "form"
    needs_review = result.get("needs_review") or []
    review_count = (len(needs_review) if is_form
                    else int(result.get("unclear_count") or 0))
    status = "error" if result.get("error") else "ok"

    record = {
        "source_name": source_name,
        "source_type": source_type,
        "page": page,
        "pages_total": pages_total,
        "status": status,
        "error": result.get("error") or "",
        "duration_seconds": duration,
        "timings": result.get("timings") or {},
        "mode": result.get("mode"),
        "schema": result.get("schema") or "",
        "model": result.get("model"),
        "backend": result.get("backend"),
        "parsed": result.get("parsed"),
        "review_count": review_count,
        "needs_review": needs_review,
        "dict_version": result.get("dict_version") or "",
    }
    if AUDIT_CONTENT:
        record.update({
            "text": result.get("text") or "",
            "fields": result.get("fields") or {},
            "confidence": result.get("confidence") or {},
            "dict_status": result.get("dict_status") or {},
            "candidates": result.get("candidates") or {},
            "logprob_min": result.get("logprob_min") or {},
            "trace": result.get("trace") or {},
        })
    logging_setup.audit(record)

    storage.save({
        "request_id": logging_setup.getRequestId(),
        "source_name": source_name,
        "source_type": source_type,
        "page": page,
        "pages_total": pages_total,
        "status": status,
        "error": result.get("error") or "",
        "duration_seconds": duration,
        "mode": result.get("mode") or "",
        "schema_name": result.get("schema") or "",
        "text": result.get("text") or "",
        "fields": result.get("fields") or {},
        "confidence": result.get("confidence") or {},
        "dict_status": result.get("dict_status") or {},
        "trace": result.get("trace") or {},
        # В текстовом режиме очередь проверки — это число неразборчивых
        # слов; список полей есть только у схемы.
        "needs_review": (needs_review if is_form
                         else ["text"] * min(review_count, 1)),
        "dict_version": result.get("dict_version") or "",
        "model_name": result.get("model") or "",
    })


def _error(message, code):
    return jsonify({"success": False, "error": message,
                    "request_id": logging_setup.getRequestId()}), code


# === ЖУРНАЛИРОВАНИЕ ЗАПРОСОВ ===

@app.before_request
def beginRequest():
    """Заводит request_id и пишет приход запроса."""
    incoming = (request.headers.get("X-Request-ID") or "").strip()
    # чужой идентификатор идёт в имена файлов сырого вывода — чистим
    safe = "".join(c for c in incoming if c.isalnum() or c in "-_")[:64]
    g.request_id = logging_setup.setRequestId(
        safe or logging_setup.newRequestId())
    g.started = time.time()

    # статику не логируем, её десятки на одну страницу
    if request.path.startswith("/api") or request.method == "POST":
        log.info("запрос принят", extra={
            "method": request.method,
            "path": request.path,
            "client_ip": request.remote_addr,
            "content_length": request.content_length or 0,
        })


@app.after_request
def endRequest(response):
    """Проставляет request_id в ответ и пишет исход."""
    request_id = getattr(g, "request_id", "") or logging_setup.getRequestId()
    if request_id:
        response.headers["X-Request-ID"] = request_id

    if request.path.startswith("/api") or request.method == "POST":
        log.info("ответ отправлен", extra={
            "method": request.method,
            "path": request.path,
            "status": response.status_code,
            "seconds": round(time.time() - getattr(g, "started", time.time()), 3),
        })
    return response


@app.errorhandler(Exception)
def handleUnexpected(error):
    """Необработанное исключение: стектрейс в журнал, клиенту request_id."""
    # 404, 405, 413 — штатные ответы фреймворка, не аварии
    if isinstance(error, HTTPException):
        log.warning("запрос отклонён", extra={
            "path": request.path, "status": error.code,
            "reason": error.description})
        return _error(error.description, error.code)

    log.error("необработанная ошибка обработки запроса",
              extra={"path": request.path, "method": request.method,
                     "error": str(error)}, exc_info=True)
    return _error("внутренняя ошибка сервиса", 500)


# === РОУТЫ ===

@app.route("/")
def index():
    """Веб-интерфейс."""
    return render_template("index.html", pdf_support=HAS_FITZ)


@app.route("/api/health")
def health():
    """Живость сервиса
    ---
    tags: [Служебные]
    summary: Жив ли процесс
    description: >
      Отвечает `ok`, пока процесс обслуживает запросы. Готовность к
      распознаванию сюда намеренно не входит: иначе healthcheck Docker
      перезапускал бы контейнер из-за отсутствующего файла модели.
      Готовность смотрите в `/api/status`.
    responses:
      200:
        description: Сервис жив
    """
    return jsonify({
        "status": "ok",
        "message": "сервис работает",
        "request_id": logging_setup.getRequestId(),
    })


@app.route("/api/config")
def getConfig():
    """Текущая конфигурация
    ---
    tags: [Служебные]
    summary: Действующие настройки сервиса
    description: >
      Настройки так, как их прочитал сервис, с учётом переменных окружения.
      Полезно, чтобы убедиться, что контейнер поднялся с тем конфигом,
      который вы правили.
    responses:
      200:
        description: Конфигурация
    """
    return jsonify(config)


@app.route("/api/status")
def status():
    """Детальный статус сервиса
    ---
    tags: [Служебные]
    summary: Готовность к работе, железо, схемы, словари, история
    description: >
      Главная диагностическая ручка. `ready: false` означает, что
      распознавание вернёт 503 — смотрите `model_exists` и `llama_exists`,
      чтобы понять, чего не хватает.
    responses:
      200:
        description: Состояние сервиса
    """
    return jsonify({
        "ready": engine.ready(),
        "model_exists": engine.modelExists(),
        "model_path": engine.model_path,
        "mmproj_path": engine.mmproj_path,
        "llama_exists": engine.llamaExists(),
        "llama_path": engine.llama_path,
        "backend": engine.backend,
        "backend_configured": config["ocr"]["backend"],
        "pdf_support": HAS_FITZ,
        "hardware": engine.hardware,
        "model_name": engine.model_name,
        "gpu_layers": engine.gpu_layers,
        "schemas": sorted(schemas.listSchemas()),
        "dictionaries": {
            "loaded": dictmatch.dbLoaded(),
            "version": dictmatch.dictVersion(),
            "sizes": {k: len(v) for k, v in dictmatch.DB.items()},
            "errors": list(dictmatch.LOAD_ERRORS),
        },
        "storage": storage.stats(),
        "logs": {
            "dir": logging_setup.LOG_DIR,
            "raw_model_output": logging_setup.LOG_RAW,
            "audit_content": AUDIT_CONTENT,
        },
        "request_id": logging_setup.getRequestId(),
    })


@app.route("/api/schemas")
def listSchemas():
    """Список схем
    ---
    tags: [Схемы]
    summary: Какие схемы документов загружены
    description: >
      Схемы читаются из `config/schemas/*.json`; имя файла без расширения —
      имя схемы для параметра `schema`. Файлы, не прошедшие проверку, видны
      в `errors` с причиной.
    responses:
      200:
        description: Загруженные схемы и ошибки загрузки
    """
    loaded = schemas.listSchemas()
    return jsonify({
        "dir": schemas.SCHEMA_DIR,
        "schemas": [schemas.describe(loaded[name]) for name in sorted(loaded)],
        "errors": schemas.loadErrors(),
        "request_id": logging_setup.getRequestId(),
    })


@app.route("/api/schemas/<name>")
def schemaDetail(name):
    """Одна схема
    ---
    tags: [Схемы]
    summary: Схема целиком, вместе с промптом полностраничного прохода
    parameters:
      - {name: name, in: path, type: string, required: true}
    responses:
      200:
        description: Схема
      404:
        description: Схемы с таким именем нет
        schema: {$ref: "#/definitions/Error"}
    """
    sch = schemas.get(name)
    if sch is None:
        return _error("схема не найдена", 404)
    return jsonify({"success": True, "schema": sch,
                    "prompt": schemas.fullPrompt(sch),
                    "request_id": logging_setup.getRequestId()})
