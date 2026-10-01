<div align="center">

# Margin

**Распознавание рукописного текста на своей видеокарте**

Сканы, фотографии и PDF → расшифровка рукописи или поля документа по вашей схеме.<br>
Qwen2.5-VL через llama.cpp, локально: документы никуда не уходят.

[![CI](https://github.com/boomchik93/Margin/actions/workflows/ci.yml/badge.svg)](https://github.com/boomchik93/Margin/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.11](https://img.shields.io/badge/python-3.11-blue.svg)
![llama.cpp](https://img.shields.io/badge/llama.cpp-CUDA-green.svg)

[Быстрый старт](#быстрый-старт) ·
[Использование](#использование) ·
[API](docs/API.md) ·
[Схемы](docs/SCHEMAS.md) ·
[Настройки](docs/CONFIGURATION.md) ·
[Архитектура](docs/ARCHITECTURE.md)

</div>

---

## Что это

Margin — HTTP-сервис с веб-интерфейсом, который читает то, что написано от
руки. Два режима:

| Режим | Что на входе | Что на выходе |
|---|---|---|
| **Свободный текст** | Письмо, записка, конспект, страница тетради | Текст построчно; неразборчивые слова помечены `[?]` |
| **Поля по схеме** | Анкета, заявление, бланк с печатными подписями полей | JSON с полями из вашей схемы, уверенность по каждому, список полей на проверку |

```bash
curl -F "file=@letter.jpg" http://localhost:5002/api/ocr
```

```json
{
  "success": true,
  "mode": "text",
  "text": "Привет!\nПишу тебе из Казани.",
  "lines": ["Привет!", "Пишу тебе из Казани."],
  "unclear_count": 0,
  "request_id": "a3f2c1d0"
}
```

## Возможности

- **Только рукописное.** Печатные подписи полей не попадают в значения.
  Нужен и печатный текст — один параметр `printed=1`.
- **Понимает расположение.** Vision-language модель видит страницу целиком и
  знает, к какому полю относится запись, а не возвращает плоский текст.
- **Не выдумывает.** Пустое поле остаётся пустым, неразборчивое слово
  помечается, невозможная дата не «чинится», а уходит на ручную проверку.
- **Схема вместо кода.** Новый вид документа — это JSON-файл с полями и
  типами: `text`, `number`, `date`, `phone`, `checkbox`.
- **Словари.** Значение сверяется со списком допустимых: описка в одну-две
  похожие буквы исправляется, неизвестное значение уходит на проверку с
  ближайшими кандидатами.
- **Уверенность и очередь проверки.** Оценка 0..100 и причина по каждому
  полю; сомнительные поля собраны в `needs_review`.
- **Перечитывает трудные места.** Зона документа читается отдельным крупным
  фрагментом, номер или код — тремя проходами с посимвольным голосованием.
- **Помнит, что вернул.** История в SQLite, структурированные журналы,
  сквозной `request_id`, трассировка каждого поля по стадиям.
- **Папка вместо API.** Положили файл в `data/in` — забрали JSON из
  `data/out`.

## Быстрый старт

**Нужно:** Linux, NVIDIA GPU от 8 ГБ VRAM (лучше 11+), Docker с Compose и
NVIDIA Container Toolkit, ~20 ГБ на диске.

```bash
git clone git@github.com:boomchik93/Margin.git
cd Margin

cp .env.example .env
pip install huggingface-hub && scripts/download_model.sh   # модель, ~6 ГБ
docker compose up -d --build                               # первая сборка долгая: компилируется llama.cpp

curl -s http://localhost:5002/api/status                   # "ready": true — можно работать
```

Веб-интерфейс — http://localhost:5002, Swagger — http://localhost:5002/apidocs/.

<details>
<summary><b>Без Docker</b></summary>

Нужны Python 3.11 и собранный [llama.cpp](https://github.com/ggerganov/llama.cpp)
(`llama-mtmd-cli`, по желанию `llama-server`) в `llama.cpp/build/bin/` либо
путь к нему в `LLAMA_CLI_PATH`.

```bash
pip install -r requirements.txt
scripts/download_model.sh
python src/app.py
```

</details>

<details>
<summary><b>Без видеокарты</b></summary>

Сервис работает и на CPU, но страница читается минутами вместо секунд.
Годится, чтобы проверить развёртывание, а не для потока документов.

</details>

## Использование

### Свободный текст

```bash
curl -F "file=@note.jpg" http://localhost:5002/api/ocr
curl -F "file=@page.png" -F "printed=1" http://localhost:5002/api/ocr    # и печатный текст тоже
curl -F "file=@note.jpg" -F "language=en" http://localhost:5002/api/ocr  # рукопись на английском
```

### Поля по схеме

Опишите документ в `config/schemas/<имя>.json`:

```json
{
  "title": "Анкета участника",
  "fields": [
    {"key": "full_name", "label": "Фамилия, имя, отчество", "case": "upper", "required": true},
    {"key": "birth_date", "label": "Дата рождения", "type": "date"},
    {"key": "phone", "label": "Контактный телефон", "type": "phone"},
    {"key": "city", "label": "Город", "dictionary": "cities", "max_dist": 1.0},
    {"key": "consent", "label": "Согласие на обработку данных", "type": "checkbox"}
  ]
}
```

и передайте её имя:

```bash
curl -F "file=@form.pdf" -F "schema=example_form" http://localhost:5002/api/ocr
```

Ответ (сокращён до полей из примера выше):

```json
{
  "success": true,
  "mode": "form",
  "schema": "example_form",
  "fields": {
    "full_name": "ПЕТРОВА АННА СЕРГЕЕВНА",
    "birth_date": "14.03.1991",
    "phone": "89001234567",
    "city": "Казань",
    "consent": "checked"
  },
  "confidence": {"full_name": 55, "birth_date": 70, "phone": 70, "city": 55, "consent": 70},
  "dict_status": {"city": "exact"},
  "needs_review": []
}
```

Схема подхватывается без перезапуска. Её можно передать и прямо в запросе —
полем `schema_json`. Полный формат, зоны и словари —
[docs/SCHEMAS.md](docs/SCHEMAS.md).

### Папочная обвязка

```
data/in/scan.pdf  ─►  data/out/scan.json  +  data/in/archive/scan.pdf
```

```bash
docker compose --profile watcher up -d margin-watcher      # демон в Docker
docker compose --profile watcher rm -sf margin-watcher     # остановить

python tools/watch_folder.py --in data/in --out data/out --schema example_form --once
```

Файл берётся только дописанным, результат появляется в `out/` целиком,
оригинал уезжает в архив даже после сбоя. Обвязка держит свой
`llama-server`, поэтому не поднимается вместе с `margin`: на одной карте они
делили бы видеопамять.

## Как это работает

```
файл ─► PDF в страницы ─► предобработка ─► модель: вся страница
                         (апскейл, CLAHE,          │
                          шумоподавление)          ├─► зоны схемы: крупные фрагменты
                                                   ├─► голосование по трудным полям
                                                   ▼
                       нормализация ─► словари ─► уверенность ─► очередь проверки
                                                   │
                                                   ▼
                                      JSON  +  история  +  журнал
```

Модель вызывается через `llama-mtmd-cli` или постоянный `llama-server`; второй
быстрее под нагрузкой и отдаёт вероятности чтения (`logprob_min`). Подробно,
с причинами решений, — [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Структура

```
src/                  сервис и пайплайн
├─ app.py             HTTP API и веб-интерфейс
├─ ocr.py             предобработка, вызовы модели, зоны, голосование
├─ schema.py          схемы полей и промпты
├─ normalize.py       нормализация значений
├─ dictmatch.py       словари и подбор по похожести
├─ confidence.py      уверенность и очередь проверки
└─ storage.py         история в SQLite
config/schemas/       схемы документов
data/dictionaries/    словари допустимых значений
tools/watch_folder.py папочная обвязка
tests/unit/           модульные тесты
docs/                 документация
```

## Разработка

```bash
pip install -r requirements.txt ruff
python -m unittest discover -s tests/unit    # модель и GPU не нужны: модель подменена заглушкой
ruff check .
```

CI на каждый пуш и pull request: ruff, ShellCheck, проверка JSON-конфигов и
модульные тесты на Python 3.11.

## Ограничения

- **Качество скана решает больше настроек.** 300 DPI читается заметно лучше
  100 DPI: на низком разрешении рукописные `4` и `9`, `1` и `7` неразличимы.
- **Страница должна лежать ровно.** Автоповорота пока нет.
- **Зоны привязаны к вёрстке.** Они задаются в долях страницы и рассчитаны на
  одинаково отсканированные документы одного вида.
- **Модель ошибается.** Уверенность и `needs_review` сужают круг проверки, но
  не заменяют её там, где цена ошибки высока.

## Безопасность и данные

Аутентификации нет: Margin рассчитан на работу внутри своей сети. Наружу —
только за reverse-proxy с авторизацией, заготовка в
[config/nginx.conf.example](config/nginx.conf.example).

Распознанный текст сохраняется в `data/results.db` и `logs/audit.jsonl`. Для
документов с персональными данными эти директории требуют той же защиты, что
и сами документы; хранение отключается переменными `RESULTS_DB_ENABLED=0` и
`AUDIT_LOG_CONTENT=0`.

## Стек

Qwen2.5-VL-7B-Instruct (Q4_K_M) · llama.cpp с CUDA · Flask · Swagger UI ·
PyMuPDF · OpenCV · Pillow · SQLite

## Лицензия

[MIT](LICENSE)
