#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Папочная обвязка: забор из in/, результат в out/, оригинал в in/archive/.

Демон следит за папкой in/. Появился файл — режем его на страницы, читаем
каждую и пишем один JSON на исходный файл:

    in/scan-001.pdf  ->  out/scan-001.json + in/archive/scan-001.pdf

Запуск:

    python tools/watch_folder.py --in data/in --out data/out
    python tools/watch_folder.py --in data/in --out data/out --schema example_form

Модель и llama-server поднимаются один раз на весь срок жизни демона:
загрузка занимает секунды, платить их на каждом файле незачем.
"""
import os
import sys
import json
import time
import shutil
import argparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import logging_setup  # noqa: E402
logging_setup.setup()

log = logging_setup.getLogger("watch")

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif")
PDF_EXT = (".pdf",)
SUPPORTED = IMAGE_EXT + PDF_EXT

# Файл считается дописанным, когда размер не менялся столько секунд подряд.
# Копирование по сети идёт кусками, и без этой выдержки демон заберёт
# половину PDF и напишет мусор в out/.
SETTLE_SECONDS = 2.0
# Пауза между обходами папки. Ниже секунды смысла нет: чтение страницы всё
# равно занимает секунды-десятки секунд.
POLL_SECONDS = 1.0


def isSupported(name):
    return name.lower().endswith(SUPPORTED)


def sizeOf(path):
    """Размер файла, или None если он исчез."""
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def stableFiles(paths, settle=None):
    """Отбирает из paths те, что дописаны: размер не менялся settle секунд.

    Выдержка одна на весь список, а не на файл: партию из полусотни сканов
    кладут во вход разом, и посекундная проверка каждого съедала бы минуты
    на файлах, которые давно лежат.

    settle читается из SETTLE_SECONDS в момент вызова, а не берётся значением
    по умолчанию: иначе переопределить выдержку (в тестах или на ходу) стало
    бы нельзя — аргумент по умолчанию считается один раз при импорте.
    """
    if settle is None:
        settle = SETTLE_SECONDS
    before = {p: sizeOf(p) for p in paths}
    if not before:
        return []
    time.sleep(settle)
    return [p for p in paths
            if before[p] and sizeOf(p) == before[p]]


def pickFiles(in_dir, names, skipped):
    """Что из папки берём в работу на этом обходе.

    `skipped` — имена посторонних файлов, о которых в журнале уже сказано.
    Функция его правит: посторонний файл остаётся лежать во входе, а обход
    идёт раз в секунду, и жаловаться на него каждую секунду нельзя — журнал
    утонет в одной и той же строке.
    """
    candidates = []
    for name in names:
        src = os.path.join(in_dir, name)
        if name.startswith(".") or not os.path.isfile(src):
            continue
        if not isSupported(name):
            if name not in skipped:
                skipped.add(name)
                log.warning("формат не поддерживается, файл пропущен",
                            extra={"source_file": name})
            continue
        candidates.append(src)

    # Дописанность проверяется у всех разом: одна выдержка на обход, а не на
    # файл. Ещё копирующийся файл просто не попадёт в отбор, и мы вернёмся к
    # нему на следующем обходе.
    ready = set(stableFiles(candidates))

    # Забываем то, чего во входе больше нет: если посторонний файл уберут и
    # положат снова, о нём стоит сказать заново.
    skipped &= set(names)
    return [os.path.basename(p) for p in candidates if p in ready]


def uniquePath(path):
    """Свободное имя рядом с path: archive/ хранит несколько заездов одного файла."""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    candidate = "%s.%s%s" % (stem, stamp, ext)
    n = 1
    while os.path.exists(candidate):
        candidate = "%s.%s-%d%s" % (stem, stamp, n, ext)
        n += 1
    return candidate
