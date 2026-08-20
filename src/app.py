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
# Порядок ключей сохраняется: поля идут в том порядке, в каком описаны в
# схеме, а не по алфавиту.
app.json.sort_keys = False

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


@app.route("/api/dictionaries")
def dictionaries():
    """Состояние словарей
    ---
    tags: [Словари]
    summary: Куда смотрит сервис и что там нашёл
    description: >
      Отвечает на вопрос «я положил файл в директорию, он подхватился?».
      Показывает путь, версию, число значений по каждому словарю и ошибки
      разбора файлов.
    responses:
      200:
        description: Состояние словарей
    """
    return jsonify(dictmatch.status())


@app.route("/api/dictionaries/reload", methods=["POST"])
def reloadDictionaries():
    """Перечитать словари с диска
    ---
    tags: [Словари]
    summary: Подхватить изменения без перезапуска
    description: >
      Рестарт сервиса стоит времени: модель заново раскладывается по
      видеопамяти. Перезагрузка словарей занимает доли секунды и не трогает
      ни модель, ни текущие соединения. Обычно не нужна: сервис сам замечает
      изменение файлов перед следующей страницей.
    responses:
      200:
        description: Словари перечитаны
      500:
        description: Не удалось перечитать, работают старые данные
        schema: {$ref: "#/definitions/Error"}
    """
    try:
        state = dictmatch.reload()
    except Exception as e:
        log.error("перезагрузка словарей не удалась",
                  extra={"error": str(e)}, exc_info=True)
        return _error("не удалось перезагрузить словари", 500)

    return jsonify({"success": True, "dictionaries": state,
                    "request_id": logging_setup.getRequestId()})


@app.route("/api/results")
def results():
    """История распознаваний
    ---
    tags: [История]
    summary: Список сохранённых результатов, новые первыми
    description: Фильтры складываются по И.
    parameters:
      - {name: limit, in: query, type: integer, default: 50, maximum: 500}
      - {name: offset, in: query, type: integer, default: 0}
      - {name: date_from, in: query, type: string, format: date,
         description: "Нижняя граница даты, ГГГГ-ММ-ДД"}
      - {name: date_to, in: query, type: string, format: date,
         description: "Верхняя граница даты, ГГГГ-ММ-ДД"}
      - {name: q, in: query, type: string,
         description: Подстрока в распознанном тексте или значениях полей}
      - {name: schema, in: query, type: string,
         description: Только результаты по этой схеме}
      - {name: status, in: query, type: string, enum: [ok, error]}
      - {name: needs_review, in: query, type: string, enum: ["1", "true", "yes"],
         description: Только записи, требующие ручной проверки}
    responses:
      200:
        description: Страница истории
      400:
        description: limit или offset не число
        schema: {$ref: "#/definitions/Error"}
    """
    try:
        limit = max(1, min(int(request.args.get("limit", 50)), 500))
        offset = max(int(request.args.get("offset", 0)), 0)
    except ValueError:
        return _error("limit и offset должны быть числами", 400)

    data = storage.listResults(
        limit=limit,
        offset=offset,
        date_from=request.args.get("date_from"),
        date_to=request.args.get("date_to"),
        query=request.args.get("q"),
        schema_name=request.args.get("schema"),
        status=request.args.get("status"),
        needs_review_only=request.args.get("needs_review") in ("1", "true", "yes"),
    )
    return jsonify({"success": "error" not in data, "limit": limit,
                    "offset": offset, **data})


@app.route("/api/results/<request_id>")
def resultDetail(request_id):
    """Одна запись истории
    ---
    tags: [История]
    summary: Все страницы одного запроса с трассировкой стадий
    parameters:
      - {name: request_id, in: path, type: string, required: true}
    responses:
      200:
        description: Запись найдена
      404:
        description: Записи с таким request_id нет
        schema: {$ref: "#/definitions/Error"}
    """
    items = storage.getByRequest(request_id)
    if not items:
        return jsonify({"success": False, "error": "запись не найдена",
                        "target_request_id": request_id,
                        "request_id": logging_setup.getRequestId()}), 404
    return jsonify({"success": True, "target_request_id": request_id,
                    "pages": len(items), "results": items})


@app.route("/upload", methods=["POST"])
@app.route("/api/ocr", methods=["POST"])
def upload():
    """Распознать файл
    ---
    tags: [Распознавание]
    summary: Загрузить изображение или PDF и получить текст либо поля
    description: >
      Основная ручка сервиса. Файл передаётся как `multipart/form-data` в
      поле `file`.


      Без `schema` возвращается расшифровка рукописного текста страницы, со
      `schema` — поля документа по схеме. Для изображения результат лежит в
      корне ответа, для PDF — в массиве `results`, по элементу на страницу;
      различать удобно по полю `type`.


      Обработка небыстрая: страница занимает от секунд до десятков секунд в
      зависимости от железа и числа зон в схеме. Ставьте таймаут клиента с
      запасом.
    consumes: [multipart/form-data]
    parameters:
      - name: file
        in: formData
        type: file
        required: true
        description: png, jpg, jpeg, gif, bmp, webp или pdf
      - name: schema
        in: formData
        type: string
        description: Имя схемы из /api/schemas. Без него — свободный текст
      - name: schema_json
        in: formData
        type: string
        description: Схема прямо в запросе, JSON в формате docs/SCHEMAS.md
      - name: language
        in: formData
        type: string
        description: >
          Язык рукописи для свободного текста: ru, en, auto или название
          языка. По умолчанию — из настроек
      - name: printed
        in: formData
        type: boolean
        description: Свободный текст — читать и печатный текст тоже
      - name: X-Request-ID
        in: header
        type: string
        description: Свой идентификатор запроса
    responses:
      200:
        description: Файл обработан
        schema:
          type: object
          properties:
            success: {type: boolean}
            type: {type: string, enum: [image, pdf]}
            request_id: {type: string}
            duration_seconds: {type: number}
            pages: {type: integer, description: "Только для PDF"}
            results:
              type: array
              description: Страницы PDF, только при type=pdf
              items: {$ref: "#/definitions/PageResult"}
      400:
        description: Нет файла, неподдерживаемый формат или неверная схема
        schema: {$ref: "#/definitions/Error"}
      413:
        description: Файл больше допустимого размера
        schema: {$ref: "#/definitions/Error"}
      429:
        description: Превышен лимит запросов в минуту
        schema: {$ref: "#/definitions/Error"}
      502:
        description: Модель не ответила
        schema: {$ref: "#/definitions/Error"}
      503:
        description: Нет файла модели или llama.cpp
        schema: {$ref: "#/definitions/Error"}
    """
    if not checkRateLimit():
        log.warning("запрос отклонён лимитом", extra={
            "client_ip": request.remote_addr,
            "limit_per_minute":
                config["api"]["rate_limit"]["requests_per_minute"]})
        return _error("слишком много запросов", 429)

    if "file" not in request.files:
        return _error("нет файла", 400)

    file = request.files["file"]
    if file.filename == "":
        return _error("пустое имя файла", 400)

    if not allowedFile(file.filename):
        log.warning("формат файла не поддерживается", extra={
            "source_file": file.filename,
            "allowed": sorted(ALLOWED_EXTENSIONS)})
        return _error("неподдерживаемый формат", 400)

    sch, schema_error = resolveSchema()
    if schema_error:
        return _error(schema_error, 400)

    if not engine.modelExists():
        log.error("файл модели не найден, распознавание невозможно",
                  extra={"model_path": engine.model_path,
                         "mmproj_path": engine.mmproj_path})
        return _error("модель не найдена", 503)

    if not engine.llamaExists():
        log.error("llama.cpp не найден, распознавание невозможно",
                  extra={"llama_path": engine.llama_path})
        return _error("llama.cpp не найден", 503)

    extension = file.filename.rsplit(".", 1)[1].lower()
    if extension == "pdf" and not HAS_FITZ:
        return _error("pdf не поддерживается: не установлен PyMuPDF", 400)

    language = (request.form.get("language") or request.args.get("language")
                or "").strip().lower() or None
    printed = _flag("printed")

    # Имя на диске не зависит от имени клиента: secure_filename вычищает
    # кириллицу до пустой строки, а расширение уже проверено.
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    safe_name = secure_filename(file.filename) or "upload"
    file_path = os.path.join(app.config["UPLOAD_FOLDER"],
                             "%s_%s.%s" % (uuid.uuid4().hex,
                                           safe_name.rsplit(".", 1)[0][:40],
                                           extension))
    file.save(file_path)

    log.info("файл принят", extra={
        "source_file": file.filename,
        "size_bytes": os.path.getsize(file_path),
        "mode": "form" if sch else "text",
        "schema": sch["name"] if sch else "",
    })

    started = time.time()
    request_id = logging_setup.getRequestId()
    try:
        if extension == "pdf":
            images = extractImagesFromPdf(file_path,
                                          config["ocr"]["pdf_scale"])
            if not images:
                return _error("не удалось извлечь страницы", 400)

            entries = []
            for img in images:
                page_started = time.time()
                try:
                    result = engine.readPage(img["path"], sch,
                                             language=language,
                                             printed=printed)
                finally:
                    try:
                        os.remove(img["path"])
                    except OSError:
                        pass
                duration = round(time.time() - page_started, 2)
                recordPage(result, file.filename, "pdf", img["page"],
                           len(images), duration)
                entries.append(pageEntry(result, page=img["page"]))

            total = round(time.time() - started, 2)
            log.info("PDF обработан", extra={
                "source_file": file.filename, "pages": len(entries),
                "seconds": total,
                "pages_failed": sum(1 for e in entries if e.get("error"))})

            # Модель не ответила ни на одной странице — это отказ сервиса, а
            # не результат. Частичный успех отдаётся как есть: страницы со
            # сбоем помечены `error`.
            if all(e.get("error") for e in entries):
                return _error("распознавание не удалось", 502)
            return jsonify({
                "success": True,
                "type": "pdf",
                "pages": len(entries),
                "results": entries,
                "request_id": request_id,
                "duration_seconds": total,
            })

        result = engine.readPage(file_path, sch, language=language,
                                 printed=printed)
        duration = round(time.time() - started, 2)
        recordPage(result, file.filename, "image", 1, 1, duration)
        if result.get("error"):
            return _error("распознавание не удалось", 502)

        payload = {"success": True, "type": "image",
                   "request_id": request_id, "duration_seconds": duration}
        payload.update(pageEntry(result))
        return jsonify(payload)

    finally:
        try:
            os.remove(file_path)
        except OSError as e:
            log.warning("загруженный файл не удалён",
                        extra={"path": file_path, "error": str(e)})


def main():
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

    log.info("сервис запускается", extra={
        "model": engine.model_name,
        "model_present": engine.modelExists(),
        "llama_path": engine.llama_path,
        "llama_present": engine.llamaExists(),
        "backend": config["ocr"]["backend"],
        "device": engine.hardware["device"],
        "gpu_layers": engine.gpu_layers,
        "pdf_support": HAS_FITZ,
        "host": config["server"]["host"],
        "port": config["server"]["port"],
        "schemas": sorted(schemas.listSchemas()),
        "dictionaries_dir": dictmatch.DICT_DIR,
        "log_dir": logging_setup.LOG_DIR,
        "results_db": storage.DB_PATH,
    })

    if not engine.modelExists():
        log.error("файл модели или проектора отсутствует: сервис поднимется, "
                  "но каждый запрос распознавания вернёт 503",
                  extra={"model_path": engine.model_path,
                         "mmproj_path": engine.mmproj_path})
    if not engine.llamaExists():
        log.error("llama.cpp отсутствует: сервис поднимется, но каждый "
                  "запрос распознавания вернёт 503",
                  extra={"llama_path": engine.llama_path})

    app.run(host=config["server"]["host"], port=config["server"]["port"],
            debug=config["server"]["debug"])


if __name__ == "__main__":
    main()
