# HTTP API

Базовый адрес — `http://<хост>:5002`. Все ответы — JSON в UTF-8.
Интерактивная версия этого документа — Swagger на `/apidocs/`.

| Метод | Путь | Назначение |
|---|---|---|
| `POST` | `/api/ocr` | Распознать файл |
| `GET` | `/api/schemas` | Список схем |
| `GET` | `/api/schemas/{name}` | Одна схема и её промпт |
| `GET` | `/api/dictionaries` | Состояние словарей |
| `POST` | `/api/dictionaries/reload` | Перечитать словари |
| `GET` | `/api/results` | История распознаваний |
| `GET` | `/api/results/{request_id}` | Одна запись истории |
| `GET` | `/api/status` | Готовность, железо, состав |
| `GET` | `/api/health` | Живость процесса |
| `GET` | `/api/config` | Действующие настройки |
| `GET` | `/` | Веб-интерфейс |

---

## Общие правила

**Идентификатор запроса.** Каждый ответ несёт `request_id` в теле и в
заголовке `X-Request-ID`. Можно прислать свой в заголовке `X-Request-ID`:
берутся буквы, цифры, `-` и `_`, до 64 символов. По идентификатору
собирается история обработки в журнале и находится запись в
`/api/results/{request_id}`.

**Ошибки.** Любая ошибка — JSON вида:

```json
{"success": false, "error": "неподдерживаемый формат", "request_id": "a3f2c1d0"}
```

Текст внутренней ошибки наружу не отдаётся: в исключениях бывают пути
файловой системы и куски распознанных данных. Подробности — в журнале по
`request_id`.

**Время ответа.** Страница читается от секунд до десятков секунд, PDF —
постранично, время складывается. Ставьте таймаут клиента с запасом.

---

## POST /api/ocr

Распознаёт файл. Тот же обработчик доступен как `POST /upload`.

Запрос — `multipart/form-data`:

| Поле | Обязательно | Описание |
|---|---|---|
| `file` | да | `png`, `jpg`, `jpeg`, `gif`, `bmp`, `webp` или `pdf` |
| `schema` | нет | Имя схемы из `/api/schemas`. Без него — свободный текст |
| `schema_json` | нет | Схема прямо в запросе, JSON в формате [SCHEMAS.md](SCHEMAS.md). Важнее `schema` |
| `language` | нет | Свободный текст: язык рукописи — `ru`, `en`, `auto` или название языка. По умолчанию из настроек |
| `printed` | нет | Свободный текст: `1` — читать и печатный текст тоже |

`schema`, `language` и `printed` принимаются и в строке запроса.

### Ответ: свободный текст

```json
{
  "success": true,
  "type": "image",
  "mode": "text",
  "parsed": true,
  "text": "Купить:\nхлеб\n[?] и молоко",
  "lines": ["Купить:", "хлеб", "[?] и молоко"],
  "unclear_count": 1,
  "backend": "cli",
  "model": "Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf",
  "timings": {"preprocess": 0.4, "model": 5.8, "total": 6.2},
  "request_id": "a3f2c1d0",
  "duration_seconds": 6.2
}
```

| Поле | Описание |
|---|---|
| `text` | Текст страницы, строки разделены `\n` |
| `lines` | Те же строки списком, сверху вниз |
| `unclear_count` | Сколько слов модель не смогла прочитать и заменила знаком `[?]` |
| `parsed` | `false` — ответ модели не разобрался как JSON; текст отдан как есть, построчно |
| `line_logprob_min` | Уверенность чтения по строкам. Только при `backend: server` |

Пустая страница — это `lines: []` с кодом 200, а не ошибка.

### Ответ: поля по схеме

```json
{
  "success": true,
  "type": "image",
  "mode": "form",
  "schema": "example_form",
  "parsed": true,
  "fields": {
    "full_name": "ПЕТРОВА АННА СЕРГЕЕВНА",
    "birth_date": "14.03.1991",
    "phone": "89001234567",
    "city": "Казнаь",
    "participants": "3",
    "comment": "",
    "consent": "checked",
    "signed": "unclear",
    "date": "32.09.2026"
  },
  "confidence": {"full_name": 55, "birth_date": 70, "phone": 70, "city": 55,
                 "participants": 70, "comment": 0, "consent": 70,
                 "signed": 25, "date": 25},
  "confidence_reason": {"full_name": "unverified", "birth_date": "format_ok",
                        "phone": "format_ok", "city": "unverified",
                        "participants": "format_ok", "comment": "empty",
                        "consent": "mark", "signed": "unclear",
                        "date": "format_bad"},
  "dict_status": {"city": "review"},
  "candidates": {"city": [{"value": "Казань", "distance": 2.0}]},
  "needs_review": ["signed", "date", "city"],
  "backend": "cli",
  "model": "Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf",
  "timings": {"preprocess": 0.4, "full_pass": 7.1, "total": 7.5},
  "request_id": "5b1e77aa",
  "duration_seconds": 7.5
}
```

| Поле | Описание |
|---|---|
| `fields` | Значения полей. **Все ключи схемы присутствуют всегда**, нераспознанное приходит пустой строкой |
| `confidence` | Оценка по каждому полю, 0..100. `0` — поле пустое |
| `confidence_reason` | Причина оценки, см. ниже |
| `dict_status` | Результат сверки со словарём — только у полей, где словарь указан |
| `candidates` | Ближайшие значения словаря для полей со статусом `review` |
| `needs_review` | Поля на ручную проверку, худшие первыми |
| `logprob_min` | Уверенность чтения по полям. Только при `backend: server` |

**Отметки** (`type: checkbox`) принимают три значения: `checked`,
`unchecked`, `unclear`. Третье — не вежливое «нет», а отдельный исход:
разглядеть не удалось.

**Оценка уверенности** считается по формату значения:

| Оценка | Причина | Значит |
|---|---|---|
| 70 | `format_ok` | Формат проверен и соблюдён: дата календарная, в телефоне хватает цифр, число в границах |
| 70 | `mark` | Отметка различима |
| 55 | `unverified` | Свободный текст, проверить нечем |
| 25 | `format_bad` | Формат нарушен — значение почти наверняка прочитано неверно |
| 25 | `unclear` | Отметку не разглядеть |
| 0 | `empty` | Поле пустое |

Это проверка правдоподобия, а не правильности: неверно прочитанная цифра
даёт такой же «хороший» телефон. Насколько уверенно модель прочитала
значение, показывает `logprob_min`.

**Статусы словаря:**

| Статус | Значит |
|---|---|
| `exact` | Найдено; значение приведено к написанию из словаря |
| `fixed` | Исправлено на единственное ближайшее значение |
| `review` | В словаре нет; оставлено как прочитано, кандидаты — в `candidates` |
| `skip` | Сверять нечего: значение пустое |
| `no_dictionary` | Словарь указан в схеме, но файл не найден |

**В `needs_review` попадает:** нарушенный формат, неразличимая отметка,
значение, которого нет в словаре, и пустое поле с `required: true`.

### Ответ: PDF

Страницы лежат в `results`, по элементу на страницу; каждый элемент устроен
как ответ на изображение и дополнен номером `page`.

```json
{
  "success": true,
  "type": "pdf",
  "pages": 2,
  "results": [
    {"page": 1, "mode": "text", "text": "...", "lines": ["..."], "unclear_count": 0},
    {"page": 2, "mode": "text", "error": "model_no_response", "text": "", "lines": []}
  ],
  "request_id": "c0ffee12",
  "duration_seconds": 13.9
}
```

Страница, которую прочитать не удалось, помечена полем `error`; остальные
отдаются как обычно.

### Коды ответа

| Код | Когда |
|---|---|
| 200 | Файл обработан |
| 400 | Нет поля `file`, пустое имя, неподдерживаемый формат, неизвестная или неверная схема, PDF без страниц |
| 413 | Файл больше `server.max_content_length` |
| 429 | Превышен лимит запросов в минуту |
| 502 | Модель не ответила ни на одной странице |
| 503 | Нет файла модели или llama.cpp |

---

## GET /api/schemas

```json
{
  "dir": "/app/config/schemas",
  "schemas": [
    {
      "name": "example_form",
      "title": "Анкета участника",
      "language": "ru",
      "fields": [
        {"key": "full_name", "label": "Фамилия, имя, отчество", "type": "text", "required": true},
        {"key": "city", "label": "Город", "type": "text", "required": false, "dictionary": "cities"}
      ],
      "zones": []
    }
  ],
  "errors": {"broken.json": ["файл не разобран как JSON: ..."]}
}
```

Файл схемы, не прошедший проверку, не загружается и виден в `errors` с
причиной.

`GET /api/schemas/{name}` возвращает схему целиком и `prompt` — текст,
который уйдёт модели на полностраничном проходе. Удобно при отладке схемы.

---

## GET /api/dictionaries

```json
{
  "dir": "/app/data/dictionaries",
  "dir_exists": true,
  "loaded": true,
  "version": "9f1c2ab04d7e",
  "dictionaries": {"cities": {"file": "cities.json", "values": 15, "skipped": 0, "max_dist": 1.0}},
  "auto_reload": {"enabled": true, "min_interval_sec": 0.0},
  "errors": []
}
```

`version` — отпечаток содержимого словарей; он же пишется в историю рядом с
каждым результатом.

`POST /api/dictionaries/reload` перечитывает словари немедленно и возвращает
то же состояние в поле `dictionaries`. Обычно не нужен: сервис сам замечает
изменение файлов перед следующей страницей.

---

## GET /api/results

История, новые записи первыми. Одна запись — одна страница.

| Параметр | Описание |
|---|---|
| `limit` | Сколько вернуть, по умолчанию 50, не больше 500 |
| `offset` | Сколько пропустить |
| `date_from`, `date_to` | Границы даты, `ГГГГ-ММ-ДД`, включительно |
| `q` | Подстрока в распознанном тексте или значениях полей (с учётом регистра для кириллицы) |
| `schema` | Только результаты по этой схеме |
| `status` | `ok` или `error` |
| `needs_review` | `1` — только требующие ручной проверки |

```json
{
  "success": true,
  "limit": 50,
  "offset": 0,
  "total": 1,
  "items": [
    {
      "id": 17,
      "request_id": "5b1e77aa",
      "created_at": "2026-09-02T14:12:03",
      "source_name": "form.png",
      "source_type": "image",
      "page": 1,
      "pages_total": 1,
      "status": "ok",
      "error": "",
      "duration_seconds": 7.5,
      "mode": "form",
      "schema_name": "example_form",
      "preview": "",
      "review_count": 3,
      "min_confidence": 25,
      "dict_version": "9f1c2ab04d7e",
      "model_name": "Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf"
    }
  ]
}
```

`preview` — первые 200 символов текста (для режима `text`).

## GET /api/results/{request_id}

Все страницы одного запроса с полным содержимым: `text`, `fields`,
`confidence`, `dict_status`, `needs_review` и `trace`.

`trace` показывает путь каждого поля по стадиям:

```json
"trace": {
  "city": {"recognized": "казанб", "normalized": "казанб", "final": "Казань", "dict": "fixed"},
  "phone": {"recognized": "8 900 123-45-67", "normalized": "89001234567",
            "final": "89001234567", "votes": {"total": 4, "agree": 3}}
}
```

По одному итоговому значению не видно, чья это работа — модели, нормализации
или словаря; по трассировке видно.

---

## GET /api/status

Главная диагностическая ручка.

```json
{
  "ready": true,
  "model_exists": true,
  "llama_exists": true,
  "model_name": "Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf",
  "backend": "cli",
  "backend_configured": "server",
  "gpu_layers": "99",
  "pdf_support": true,
  "hardware": {"device": "gpu", "vram_total_gb": 16, "cpu_cores": 12},
  "schemas": ["example_form"],
  "dictionaries": {"loaded": true, "version": "9f1c2ab04d7e", "sizes": {"cities": 15}, "errors": []},
  "storage": {"enabled": true, "ready": true, "total": 412, "last_24h": 37, "needs_review": 58, "failed": 2}
}
```

`ready: false` — распознавание вернёт 503; смотрите `model_exists` и
`llama_exists`.

`backend` — чем чтение идёт сейчас, `backend_configured` — что выбрано в
настройках. `llama-server` поднимается при первом запросе распознавания,
поэтому сразу после старта здесь `cli` даже при выбранном `server`.

## GET /api/health

Отвечает `{"status": "ok"}`, пока процесс жив. Готовность к распознаванию
сюда намеренно не входит: иначе healthcheck Docker перезапускал бы контейнер
из-за отсутствующего файла модели.
