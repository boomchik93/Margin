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
