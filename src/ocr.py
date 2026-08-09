#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Пайплайн распознавания рукописного текста.

Два режима:

  text — свободная расшифровка: всё рукописное на странице, строка за
         строкой;
  form — извлечение полей по схеме (config/schemas/*.json): значение каждого
         поля, оценка уверенности, сверка со словарём, очередь ручной
         проверки.

Читает vision-language модель через llama.cpp. Классический OCR вернул бы
плоский текст без привязки к полям; модель, понимающая расположение, отличает
рукописное от печатного и знает, к какому полю относится запись.
"""

import json
import os
import platform
import re
import subprocess
import tempfile
import threading
import time

from PIL import Image, ImageEnhance

from logging_setup import getLogger, stage, saveRawOutput
import confidence
import dictmatch
import llamaserver
import logprobs
import normalize
import schema as schemas

log = getLogger("ocr")

try:
    import numpy as np
    import cv2
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False
    log.warning("opencv не установлен, предобработка будет ограничена: "
                "без него не работают CLAHE и шумоподавление")

try:
    import fitz
    HAS_FITZ = True
except ImportError:
    HAS_FITZ = False


# === КОНФИГ ===

# Корень проекта: src/ лежит на уровень ниже, конфиги — в config/.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Каждое имя ищется сначала в config/, потом рядом с корнем и в текущей
# директории (в Docker проект монтируется в /app).
SETTINGS_FILES = [
    os.path.join(PROJECT_ROOT, "config", "settings.json"),
    os.path.join(PROJECT_ROOT, "settings.json"),
    "settings.json",
]

DEFAULT_MODEL = "Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf"
DEFAULT_MMPROJ = "mmproj-Qwen2.5-VL-7B-Instruct-f16.gguf"


def _pick(d, *keys, default=None):
    """Первое не-None значение по списку ключей (русские и английские имена)."""
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return default


def _envOverride(value, env_name, cast=str):
    """Переопределение из переменной окружения (для Docker и .env)."""
    raw = os.environ.get(env_name)
    if raw is None or raw == "":
        return value
    try:
        return cast(raw)
    except Exception:
        return value


def defaultConfig():
    """Нормализованный конфиг по умолчанию."""
    return {
        "server": {"host": "0.0.0.0", "port": 5002, "debug": False,
                   "max_content_length": 134217728},
        # temperature=0: при ненулевой температуре повторный прогон того же
        # скана даёт другой результат, и спорный случай разобрать нечем.
        "ocr": {"timeout": None, "max_tokens": 4096, "temperature": 0,
                "context_length": 16384, "fragmentation": True,
                "language": "ru", "backend": "cli",
                "target_long_side": 2200, "pdf_scale": 2.0},
        "hardware": {"device": "auto", "gpu_layers": "auto",
                     "cpu_threads": "auto", "gpu_count": "1",
                     "force_cpu": False, "force_gpu": False},
        "paths": {"upload_folder": os.path.join(PROJECT_ROOT, "uploads"),
                  "models_folder": os.path.join(PROJECT_ROOT, "models"),
                  "llama_executable": "auto",
                  "model_file": DEFAULT_MODEL,
                  "mmproj_file": DEFAULT_MMPROJ},
        "api": {"enable_cors": True,
                "rate_limit": {"enabled": False, "requests_per_minute": 10}},
    }


def loadConfig():
    """Настройки из config/settings.json, поверх — переменные окружения."""
    d = defaultConfig()
    cfg = None
    used_file = None
    for fname in SETTINGS_FILES:
        try:
            with open(fname, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                used_file = fname
                break
        except FileNotFoundError:
            continue
        except json.JSONDecodeError as e:
            # Битый settings.json — авария конфигурации, а не мелочь: сервис
            # молча уйдёт на значения по умолчанию с другим портом.
            log.error("файл настроек не разобран, применяются значения "
                      "по умолчанию", extra={"file": fname, "error": str(e)})
            cfg = {}
            break

    if cfg is None:
        log.warning("файл настроек не найден, применяются значения "
                    "по умолчанию", extra={"searched": SETTINGS_FILES})
        cfg = {}

    srv = _pick(cfg, "сервер", "server", default={})
    server = {
        "host": _pick(srv, "хост", "host", default=d["server"]["host"]),
        "port": int(_pick(srv, "порт", "port", default=d["server"]["port"])),
        "debug": bool(_pick(srv, "отладка", "debug",
                            default=d["server"]["debug"])),
        "max_content_length": int(_pick(
            srv, "макс_размер_файла", "max_content_length",
            default=d["server"]["max_content_length"])),
    }
    server["host"] = _envOverride(server["host"], "SERVER_HOST", str)
    server["port"] = _envOverride(server["port"], "SERVER_PORT", int)

    o = _pick(cfg, "распознавание", "ocr", default={})
    ocr = {
        "timeout": _pick(o, "таймаут", "timeout", default=d["ocr"]["timeout"]),
        "max_tokens": int(_pick(o, "макс_токенов", "max_tokens",
                                default=d["ocr"]["max_tokens"])),
        "temperature": float(_pick(o, "температура", "temperature",
                                   default=d["ocr"]["temperature"])),
        "context_length": int(_pick(o, "длина_контекста", "context_length",
                                    default=d["ocr"]["context_length"])),
        "fragmentation": bool(_pick(o, "фрагментация", "fragmentation",
                                    default=d["ocr"]["fragmentation"])),
        "language": str(_pick(o, "язык", "language",
                              default=d["ocr"]["language"])).lower(),
        "backend": str(_pick(o, "движок", "backend",
                             default=d["ocr"]["backend"])).lower(),
        "target_long_side": int(_pick(o, "длинная_сторона", "target_long_side",
                                      default=d["ocr"]["target_long_side"])),
        "pdf_scale": float(_pick(o, "масштаб_pdf", "pdf_scale",
                                 default=d["ocr"]["pdf_scale"])),
    }
    ocr["backend"] = _envOverride(ocr["backend"], "LLAMA_BACKEND",
                                  lambda s: str(s).lower())

    hw = _pick(cfg, "машина", "железо", "hardware", default={})
    hardware = {
        "device": str(_pick(hw, "устройство", "device",
                            default=d["hardware"]["device"])),
        "gpu_layers": str(_pick(hw, "слои_gpu", "gpu_layers",
                                default=d["hardware"]["gpu_layers"])),
        "cpu_threads": str(_pick(hw, "потоки_cpu", "cpu_threads",
                                 default=d["hardware"]["cpu_threads"])),
        "gpu_count": str(_pick(hw, "число_gpu", "gpu_count",
                               default=d["hardware"]["gpu_count"])),
        "force_cpu": bool(_pick(hw, "только_cpu", "force_cpu",
                                default=d["hardware"]["force_cpu"])),
        "force_gpu": bool(_pick(hw, "только_gpu", "force_gpu",
                                default=d["hardware"]["force_gpu"])),
    }

    p = _pick(cfg, "пути", "paths", default={})
    paths = {
        "upload_folder": _pick(p, "папка_загрузок", "upload_folder",
                               default=d["paths"]["upload_folder"]),
        "models_folder": _pick(p, "папка_моделей", "models_folder",
                               default=d["paths"]["models_folder"]),
        "llama_executable": _pick(p, "путь_llama", "llama_executable",
                                  default=d["paths"]["llama_executable"]),
        "model_file": _pick(p, "файл_модели", "model_file",
                            default=d["paths"]["model_file"]),
        "mmproj_file": _pick(p, "файл_проектора", "mmproj_file",
                             default=d["paths"]["mmproj_file"]),
    }
    paths["model_file"] = _envOverride(paths["model_file"], "MODEL_FILE")
    paths["mmproj_file"] = _envOverride(paths["mmproj_file"], "MMPROJ_FILE")
    # Относительные пути отсчитываем от корня проекта, а не от текущей
    # директории: код запускается и из корня, и из tools/, и из Docker.
    for key in ("upload_folder", "models_folder"):
        if not os.path.isabs(paths[key]):
            paths[key] = os.path.join(PROJECT_ROOT, paths[key])

    a = _pick(cfg, "апи", "api", default={})
    rl = _pick(a, "лимит_запросов", "rate_limit", default={})
    api = {
        "enable_cors": bool(_pick(a, "включить_cors", "enable_cors",
                                  default=d["api"]["enable_cors"])),
        "rate_limit": {
            "enabled": bool(_pick(
                rl, "включён", "enabled",
                default=d["api"]["rate_limit"]["enabled"])),
            "requests_per_minute": int(_pick(
                rl, "запросов_в_минуту", "requests_per_minute",
                default=d["api"]["rate_limit"]["requests_per_minute"])),
        },
    }

    # Какие значения пришли из окружения, а какие из файла — первый вопрос
    # при разборе «почему сервис слушает не тот порт».
    overridden = [name for name in ("SERVER_HOST", "SERVER_PORT",
                                    "LLAMA_BACKEND", "MODEL_FILE",
                                    "MMPROJ_FILE")
                  if os.environ.get(name)]
    log.info("настройки загружены", extra={
        "file": used_file, "host": server["host"], "port": server["port"],
        "env_overrides": overridden, "device": hardware["device"],
        "gpu_layers": hardware["gpu_layers"], "backend": ocr["backend"],
        "fragmentation": ocr["fragmentation"], "language": ocr["language"],
        "rate_limit_enabled": api["rate_limit"]["enabled"],
    })

    return {"server": server, "ocr": ocr, "hardware": hardware,
            "paths": paths, "api": api}


def findLlama(config):
    """Путь к llama-mtmd-cli."""
    if config["paths"]["llama_executable"] != "auto":
        return config["paths"]["llama_executable"]

    if "LLAMA_CLI_PATH" in os.environ:
        return os.environ["LLAMA_CLI_PATH"]

    if platform.system() == "Windows":
        names = ["llama.cpp/build/bin/Release/llama-mtmd-cli.exe"]
    else:
        names = ["llama.cpp/build/bin/llama-mtmd-cli"]

    candidates = []
    for name in names:
        candidates.append(os.path.join(PROJECT_ROOT, name))
        candidates.append(os.path.join(".", name))
    for path in candidates:
        if os.path.exists(path):
            return path
    return candidates[0]


def _findNvidiaSmi():
    """Путь к nvidia-smi."""
    import shutil
    found = shutil.which("nvidia-smi")
    if found:
        return found
    for cand in ("/usr/lib/wsl/lib/nvidia-smi",
                 "/usr/local/nvidia/bin/nvidia-smi",
                 "/usr/bin/nvidia-smi"):
        if os.path.exists(cand):
            return cand
    return None


def getHardware(config):
    """Определяет железо и раскладку модели по нему."""
    has_nvidia = False
    nvidia_vram = 0
    nvidia_vram_total = 0
    cpu_cores = 1
    total_ram = 8

    smi = _findNvidiaSmi()
    if smi:
        try:
            result = subprocess.run(
                [smi, "--query-gpu=memory.total,memory.free",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10)
            if result.returncode == 0 and result.stdout.strip():
                has_nvidia = True
                parts = result.stdout.strip().split("\n")[0].split(",")
                total_mb = int(float(parts[0]))
                free_mb = int(float(parts[1])) if len(parts) > 1 else total_mb
                nvidia_vram = free_mb // 1024
                nvidia_vram_total = total_mb // 1024
        except Exception:
            pass

    try:
        import multiprocessing
        cpu_cores = multiprocessing.cpu_count()
    except Exception:
        pass

    try:
        import psutil
        total_ram = psutil.virtual_memory().total // (1024 ** 3)
    except Exception:
        pass

    hw = config["hardware"]
    force_cpu = hw.get("force_cpu", False)
    force_gpu = hw.get("force_gpu", False)

    # выбор устройства: явные флаги имеют приоритет, затем device, затем авто
    device = str(hw.get("device", "auto")).lower()
    if force_cpu:
        device = "cpu"
    elif force_gpu:
        device = "gpu"
    elif device in ("auto", ""):
        device = "gpu" if has_nvidia and nvidia_vram >= 4 else "cpu"
    elif device == "cuda":
        device = "gpu"

    if device == "gpu" and not has_nvidia and not force_gpu:
        # Тихий откат на CPU — худший вид отказа: сервис работает, но
        # страница обрабатывается минутами вместо секунд, и причина не видна
        # нигде.
        log.warning("запрошен GPU, но NVIDIA не найдена — работаем на CPU, "
                    "распознавание будет в разы медленнее")
        device = "cpu"

    gpu_layers = str(hw.get("gpu_layers", "auto"))
    if gpu_layers == "auto":
        # Считаем по ОБЩЕЙ памяти карты, а не по свободной в эту секунду.
        # Свободная зависит от того, что запущено рядом прямо сейчас, и
        # порог перескакивает между запусками. Частичная выгрузка на CPU не
        # просто замедляет, она роняет качество чтения. Лучше упереться в
        # нехватку памяти явно, чем молча читать хуже.
        vram = nvidia_vram_total or nvidia_vram
        if device == "cpu":
            gpu_layers = "0"
        elif vram >= 11:
            gpu_layers = "99"
        elif vram >= 8:
            gpu_layers = "35"
        elif vram >= 4:
            gpu_layers = "20"
        else:
            gpu_layers = "99" if force_gpu else "0"

        if (nvidia_vram_total and nvidia_vram
                and nvidia_vram < nvidia_vram_total * 0.6):
            log.warning("свободной видеопамяти заметно меньше общей, "
                        "модель может не поместиться целиком",
                        extra={"vram_total_gb": nvidia_vram_total,
                               "vram_free_gb": nvidia_vram,
                               "gpu_layers": gpu_layers})

    threads = str(hw.get("cpu_threads", "auto"))
    if threads == "auto":
        threads = str(max(1, cpu_cores - 1))

    result = {
        "device": device,
        "gpu_layers": str(gpu_layers),
        "cpu_threads": str(threads),
        "gpu_count": str(hw.get("gpu_count", "1")),
        "has_nvidia": has_nvidia,
        "vram_gb": nvidia_vram,
        "vram_total_gb": nvidia_vram_total,
        "cpu_cores": cpu_cores,
        "total_ram": total_ram,
    }
    log.info("железо определено", extra=dict(result))
    return result


# === PDF ===

def extractImagesFromPdf(pdf_path, scale=2.0):
    """Рендер страниц PDF в отдельные PNG. Возвращает [{"page", "path"}]."""
    if not HAS_FITZ:
        log.error("PyMuPDF не установлен, PDF обрабатывать нечем",
                  extra={"pdf": os.path.basename(pdf_path)})
        return []

    images = []
    total = 0
    with stage(log, "pdf_extract", pdf=os.path.basename(pdf_path)) as st:
        try:
            doc = fitz.open(pdf_path)
            total = len(doc)
            for page_num in range(total):
                page = doc.load_page(page_num)
                pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale))
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
                tmp.write(pix.tobytes("png"))
                tmp.close()
                images.append({"page": page_num + 1, "path": tmp.name})
            doc.close()
        except Exception as e:
            log.error("рендер PDF прерван",
                      extra={"pdf": os.path.basename(pdf_path),
                             "rendered": len(images), "expected": total,
                             "error": str(e)}, exc_info=True)
        st.add(pages_expected=total, pages_rendered=len(images))

    return images


# === ИЗВЛЕЧЕНИЕ JSON ===

_CHAT_TOKENS = ("<|im_end|>", "<|endoftext|>", "<|im_start|>")


def cleanOutput(output):
    """Ответ модели без служебных токенов чата и markdown-обёрток."""
    text = output or ""
    if "<|im_start|>assistant" in text:
        text = text.split("<|im_start|>assistant")[-1]
    for token in _CHAT_TOKENS:
        if token in text:
            text = text.split(token)[0]

    if "```json" in text:
        text = text.split("```json", 1)[1]
        text = text.split("```", 1)[0]
    elif "```" in text:
        parts = text.split("```")
        if len(parts) >= 3:
            text = parts[1]
    return text.strip()


def extractJson(output):
    """Достаёт JSON-объект из сырого вывода модели максимально устойчиво."""
    if not output:
        return None
    text = cleanOutput(output)

    # выделяем сбалансированный {...} с учётом строк и экранирования
    start = text.find("{")
    if start == -1:
        return None
    brace = 0
    end = -1
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            brace += 1
        elif ch == "}":
            brace -= 1
            if brace == 0:
                end = i + 1
                break
    if end <= start:
        return None

    candidate = text[start:end]
    try:
        return json.loads(candidate)
    except Exception:
        # пробуем подчистить хвостовые запятые
        cleaned = re.sub(r",\s*([}\]])", r"\1", candidate)
        try:
            return json.loads(cleaned)
        except Exception:
            return None


# === ПРЕДОБРАБОТКА ИЗОБРАЖЕНИЯ ===

def preprocessImage(image_path, target_long_side=2200):
    """Готовит изображение для модели: апскейл, контраст, шумоподавление.

    Возвращает путь к временному PNG либо исходный путь, если предобработка
    не удалась: исходник хуже подготовленного, но много лучше отказа.
    """
    try:
        img = Image.open(image_path)
        if img.mode != "RGB":
            img = img.convert("RGB")

        w, h = img.size
        long_side = max(w, h)
        # Апскейлим маленькие сканы, но не раздуваем уже крупные. Информации
        # апскейл не добавляет, но даёт модели больше визуальных токенов на
        # символ.
        if long_side < target_long_side:
            scale = min(target_long_side / float(long_side), 3.0)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

        if HAS_CV2:
            gray = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2GRAY)
            # CLAHE — локальное выравнивание контраста: вытягивает бледную
            # пасту, не пересвечивая фон.
            clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
            gray = clahe.apply(gray)
            # Зерно скана модель принимает за штрихи букв.
            gray = cv2.fastNlMeansDenoising(gray, None, 7, 7, 21)
            img = Image.fromarray(cv2.cvtColor(gray, cv2.COLOR_GRAY2RGB))
        else:
            img = ImageEnhance.Contrast(img).enhance(1.4)
            img = ImageEnhance.Sharpness(img).enhance(1.3)

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
        tmp.close()
        img.save(tmp.name, "PNG")
        return tmp.name
    except Exception as e:
        log.error("предобработка не удалась, распознаём исходный файл",
                  extra={"error": str(e),
                         "image": os.path.basename(image_path)}, exc_info=True)
        return image_path


def cropRegion(image_path, box):
    """Вырезает зону по долям страницы (x0, y0, x1, y1 в диапазоне 0..1)."""
    try:
        img = Image.open(image_path)
        if img.mode != "RGB":
            img = img.convert("RGB")
        w, h = img.size
        x0, y0, x1, y1 = box
        left, top = max(0, int(x0 * w)), max(0, int(y0 * h))
        right, bottom = min(w, int(x1 * w)), min(h, int(y1 * h))
        if right - left < 10 or bottom - top < 10:
            return None
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
        tmp.close()
        img.crop((left, top, right, bottom)).save(tmp.name, "PNG")
        return tmp.name
    except Exception as e:
        log.warning("кроп зоны не удался", extra={
            "error": str(e), "box": list(box),
            "image": os.path.basename(image_path)})
        return None


# === ГОЛОСОВАНИЕ ===

def charVote(values):
    """Значение по большинству голосов, посимвольно.

    Проходы ошибаются в разных символах, поэтому итог собирается по
    большинству в каждой позиции и может не совпасть ни с одним голосом
    целиком.
    """
    vals = [str(v).strip() for v in values if str(v or "").strip()]
    if not vals:
        return ""

    # Посимвольно сводятся только голоса преобладающей длины: пропущенный
    # символ сдвигает всё, что правее.
    counts = {}
    for v in vals:
        counts[len(v)] = counts.get(len(v), 0) + 1
    best_len = max(counts, key=lambda n: (counts[n], -n))
    same = [v for v in vals if len(v) == best_len]

    if len(same) < 2:
        freq = {}
        for v in vals:
            freq[v] = freq.get(v, 0) + 1
        return max(freq, key=lambda v: freq[v])

    out = []
    for i in range(best_len):
        tally = {}
        for v in same:
            tally[v[i]] = tally.get(v[i], 0) + 1
        ranked = sorted(tally.items(), key=lambda kv: -kv[1])
        if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
            # ничья: побеждает первый голос — он от самого крупного кропа
            out.append(same[0][i])
        else:
            out.append(ranked[0][0])
    return "".join(out)


def _voteKey(value):
    """Значение в виде, пригодном для сравнения голосов между собой."""
    return re.sub(r"\s+", " ", str(value or "")).strip().upper().replace("Ё", "Е")


# === СВОБОДНЫЙ ТЕКСТ ===

# Знак неразборчивого слова. Модели велено ставить его вместо догадки:
# выдуманное слово в расшифровке неотличимо от прочитанного, а знак виден.
UNCLEAR_MARK = "[?]"


def textPrompt(language="ru", printed=False):
    """Промпт свободной расшифровки страницы."""
    if printed:
        scope = ("Читай ВЕСЬ текст на изображении: и рукописный, и "
                 "напечатанный.")
    else:
        scope = ("Читай ТОЛЬКО рукописный текст (написанный от руки). "
                 "Напечатанный типографский текст пропускай целиком.")
    rules = [
        scope,
        "ТОЧНОСТЬ: переноси буквы, цифры и знаки ровно как написано. Не "
        "исправляй орфографию и пунктуацию, ничего не добавляй от себя и не "
        "пересказывай.",
        "ПОРЯДОК: сверху вниз, слева направо. Каждая строка текста на "
        "изображении — отдельный элемент списка. Строки не склеивай и не "
        "переноси слова между ними.",
        "НЕРАЗБОРЧИВОЕ: слово, которое прочитать не удалось, замени знаком "
        "%s. Не подбирай вместо него подходящее по смыслу." % UNCLEAR_MARK,
        schemas.languageRule(language),
        "Если текста для чтения на изображении нет — верни пустой список.",
        "ФОРМАТ: верни ТОЛЬКО валидный JSON. Без markdown, без ```, без "
        "пояснений.",
    ]
    return "\n".join([
        "Расшифруй текст на изображении.",
        "",
        "ПРАВИЛА:",
        "\n".join("%d. %s" % (i, r) for i, r in enumerate(rules, 1)),
        "",
        'Структура ответа: {"lines": [ ... ]} — список строк сверху вниз.',
    ])


def parseLines(raw):
    """Строки текста из ответа модели. Возвращает (строки, разобрано_ли)."""
    data = extractJson(raw)
    if isinstance(data, dict):
        if isinstance(data.get("lines"), list):
            lines = ["" if x is None else str(x) for x in data["lines"]
                     if not isinstance(x, (dict, list))]
        elif isinstance(data.get("text"), str):
            lines = data["text"].splitlines()
        else:
            lines = None
        if lines is not None:
            lines = [re.sub(r"[ \t]+", " ", s).strip() for s in lines]
            # "..." — многоточие из описания формата, которое модель иногда
            # возвращает на пустой странице вместо пустого списка.
            return [s for s in lines if s and s not in ("...", "…")], True

    # JSON не собрался: отдаём то, что модель написала, строками. Это хуже
    # разобранного ответа, но лучше пустого — текст клиент всё равно получит.
    text = cleanOutput(raw)
    return [s.strip() for s in text.splitlines() if s.strip()], False


def recognizeText(read, image_path, config, language=None, printed=False):
    """Свободная расшифровка страницы.

    `read(prompt, image_path, n_predict, tag)` — вызов модели, возвращает
    (текст, токены_с_вероятностями).
    """
    started = time.time()
    timings = {}
    temp_files = []
    language = language or config["ocr"].get("language", "ru")
    try:
        t0 = time.time()
        proc_path = preprocessImage(image_path,
                                    config["ocr"].get("target_long_side", 2200))
        timings["preprocess"] = round(time.time() - t0, 2)
        if proc_path != image_path:
            temp_files.append(proc_path)

        t0 = time.time()
        raw, tokens = read(textPrompt(language, printed), proc_path,
                           config["ocr"].get("max_tokens", 4096), "text")
        timings["model"] = round(time.time() - t0, 2)

        lines, parsed = parseLines(raw)
        if not parsed and raw:
            log.warning("ответ не разобран как JSON, текст взят как есть",
                        extra={"output_chars": len(raw),
                               "output_head": raw[:300]})

        result = {
            "mode": "text",
            "parsed": bool(parsed and raw),
            "text": "\n".join(lines),
            "lines": lines,
            "unclear_count": sum(s.count(UNCLEAR_MARK) for s in lines),
        }

        # Уверенность чтения по строкам: минимум logprob по токенам строки.
        # Есть только на пути через llama-server.
        if parsed and tokens:
            try:
                measured = logprobs.fieldLogprobs(tokens, raw)
                line_lp = [measured.get("lines[%d]" % i, (None, 0))[0]
                           for i in range(len(lines))]
                # Индексы совпадают со строками ответа только если ни одна
                # строка не была отброшена как пустая.
                raw_data = extractJson(raw) or {}
                if len(raw_data.get("lines") or []) == len(lines):
                    result["line_logprob_min"] = line_lp
            except Exception as e:
                log.debug("вероятности чтения не разобраны",
                          extra={"error": str(e)})

        timings["total"] = round(time.time() - started, 2)
        result["timings"] = timings
        log.info("расшифровка завершена", extra={
            "image": os.path.basename(image_path), "lines": len(lines),
            "unclear": result["unclear_count"], "parsed": result["parsed"],
            "timings": timings})
        return result
    finally:
        _removeFiles(temp_files)


# === ПОЛЯ ПО СХЕМЕ ===

def _mergeZone(sch, result, zdata, keys, override=False):
    """Сливает ответ зонного прохода в основной результат.

    По умолчанию зона только заполняет пустое: она дополняет общий проход,
    но не затирает его. С override ответ зоны замещает общий — это для зон,
    где кроп настолько крупнее, что читается надёжнее страницы целиком.
    """
    types = {f["key"]: f["type"] for f in sch["fields"]}
    for key in keys:
        if key not in zdata:
            continue
        if types[key] == "checkbox":
            mark = schemas.toCheckbox(zdata.get(key))
            # У отметки нет «пустого» значения, поэтому правило «непустое
            # побеждает» тут не работает. Ответ зоны принимается, когда он
            # определённее прежнего: unclear уступает checked и unchecked.
            if override or (result.get(key) == schemas.UNCLEAR
                            and mark != schemas.UNCLEAR):
                result[key] = mark
            continue
        zv = zdata.get(key)
        if isinstance(zv, (dict, list)):
            continue
        zv = "" if zv is None else str(zv).strip()
        if zv and (override or not result.get(key)):
            result[key] = zv


def recognizeForm(read, image_path, sch, config):
    """Многопроходное чтение полей: страница целиком, зоны, голосование.

    Возвращает (значения_как_прочитаны, служебные_сведения).
    """
    started = time.time()
    timings = {}
    temp_files = []
    use_zones = bool(config["ocr"].get("fragmentation", True)) and sch["zones"]
    try:
        # 1) предобработка
        t0 = time.time()
        proc_path = preprocessImage(image_path,
                                    config["ocr"].get("target_long_side", 2200))
        timings["preprocess"] = round(time.time() - t0, 2)
        if proc_path != image_path:
            temp_files.append(proc_path)

        # 2) полностраничный проход
        t0 = time.time()
        raw, tokens = read(schemas.fullPrompt(sch), proc_path, 2000, "full")
        timings["full_pass"] = round(time.time() - t0, 2)
        base = extractJson(raw)
        if base is None:
            # Модель ответила, но JSON не собрался. Отличать от «не ответила»
            # важно: первое чинится промптом, второе — железом.
            log.warning("ответ основного прохода не разобран как JSON",
                        extra={"output_chars": len(raw or ""),
                               "output_head": (raw or "")[:300]})
        result = schemas.enforce(sch, base or {})

        measured = {}
        if tokens and raw:
            try:
                measured = logprobs.fieldLogprobs(tokens, raw)
            except Exception as e:
                log.debug("вероятности чтения не разобраны",
                          extra={"error": str(e)})

        # 3) прицельные проходы по зонам: на кропе символы крупнее и
        #    читаются точнее
        zone_votes = {}
        zones_done = zones_failed = 0
        if use_zones:
            t_zones = time.time()
            for zone in sch["zones"]:
                crop_path = cropRegion(proc_path, zone["box"])
                if not crop_path:
                    zones_failed += 1
                    log.warning("кроп зоны не построен, зона пропущена",
                                extra={"zone": zone["name"]})
                    continue
                temp_files.append(crop_path)

                for n, prompt in enumerate(schemas.zonePrompts(sch, zone), 1):
                    tag = zone["name"] + ("-%d" % n if zone["vote"] else "")
                    zraw, _ = read(prompt, crop_path, 512, tag)
                    zdata = extractJson(zraw)
                    if not isinstance(zdata, dict):
                        if zraw:
                            zones_failed += 1
                            log.warning("ответ зоны не разобран как JSON",
                                        extra={"zone": tag,
                                               "output_head": zraw[:300]})
                        continue
                    zones_done += 1
                    if zone["vote"]:
                        key = zone["fields"][0]
                        val = zdata.get(key)
                        if isinstance(val, (str, int, float)) and str(val).strip():
                            zone_votes.setdefault(key, []).append(
                                str(val).strip())
                    else:
                        _mergeZone(sch, result, zdata, zone["fields"],
                                   override=zone["override"])
            timings["zones"] = round(time.time() - t_zones, 2)
            log.info("зонные проходы завершены", extra={
                "seconds": timings["zones"], "zones_total": len(sch["zones"]),
                "passes_recognized": zones_done, "passes_failed": zones_failed})

        # 4) сведение голосов. Одного голоса мало: он равноправен основному
        #    проходу, и выбрать между ними не из чего.
        votes = {}
        for key, zvotes in zone_votes.items():
            if len(zvotes) < 2:
                continue
            base_val = str(result.get(key) or "").strip()
            all_votes = zvotes + ([base_val] if base_val else [])
            merged = charVote(all_votes)
            if not merged:
                continue
            agree = sum(1 for v in all_votes if _voteKey(v) == _voteKey(merged))
            votes[key] = {"total": len(all_votes), "agree": agree}
            # Единственное место, где итог может не совпасть ни с одним
            # проходом: без списка голосов значение выглядит взявшимся
            # ниоткуда.
            log.info("значение собрано голосованием проходов", extra={
                "field": key, "votes": all_votes, "result": merged,
                "replaced": result.get(key)})
            result[key] = merged
            # Вероятность основного прохода к собранному значению больше не
            # относится: она измерена для другой строки.
            measured.pop(key, None)

        timings["total"] = round(time.time() - started, 2)
        log.info("чтение полей завершено", extra={
            "image": os.path.basename(image_path), "schema": sch["name"],
            "timings": timings})
        return result, {"parsed": base is not None, "timings": timings,
                        "votes": votes, "logprobs": measured}
    finally:
        _removeFiles(temp_files)
