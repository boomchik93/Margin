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


def toPages(src, scale):
    """Раскладывает входной файл на страницы: [(номер, путь, временный_ли)].

    ocr импортируется здесь, а не наверху модуля: он тянет Pillow и модель, а
    обход папки и перекладывание файлов должны быть проверяемы без них.
    """
    if src.lower().endswith(PDF_EXT):
        from ocr import extractImagesFromPdf
        return [(p["page"], p["path"], True)
                for p in extractImagesFromPdf(src, scale)]
    return [(1, src, False)]


def pageDocument(result, page):
    """Страница результата в том виде, в каком она ложится в out/."""
    doc = {"page": page}
    doc.update({k: v for k, v in result.items() if k != "trace"})
    return doc


def processFile(engine, src, out_dir, archive_dir, sch=None, language=None,
                printed=False, scale=2.0):
    """Файл целиком: страницы -> один JSON в out/, оригинал -> archive/."""
    name = os.path.basename(src)
    stem = os.path.splitext(name)[0]
    started = time.time()
    log.info("файл взят в обработку", extra={"source_file": name})

    try:
        pages = toPages(src, scale)
        if not pages:
            # Пустой результат в out/ не пишем: приёмник прочитал бы его как
            # «документ пустой», а на деле файл не удалось разобрать.
            # Молчание в out/ вместе с записью в журнале однозначнее.
            log.error("из файла не извлечено ни одной страницы, "
                      "результат не пишется", extra={"source_file": name})
            return False

        results = []
        for number, path, temporary in pages:
            try:
                result = engine.readPage(path, sch, language=language,
                                         printed=printed)
            except Exception as e:
                # Сбой на одной странице не должен стоить всего файла: у
                # тридцатистраничного PDF, отвалившегося на последней
                # странице, иначе пропали бы полчаса чтения. Страница уходит
                # в результат с названной причиной.
                log.error("страница не прочитана, уходит с пометкой об ошибке",
                          extra={"source_file": name, "page": number,
                                 "of": len(pages), "error": str(e)},
                          exc_info=True)
                result = {"mode": "form" if sch else "text",
                          "error": "page_failed"}
            finally:
                if temporary:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
            results.append(pageDocument(result, number))
            log.info("страница обработана", extra={
                "source_file": name, "page": number, "of": len(pages)})

        total = round(time.time() - started, 2)
        export = {
            "source_file": name,
            "processed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "mode": "form" if sch else "text",
            "schema": sch["name"] if sch else "",
            "pages": len(results),
            "pages_failed": sum(1 for r in results if r.get("error")),
            "duration_seconds": total,
            "results": results,
        }

        # Пишем через временный файл и переименовываем: приёмник может
        # читать out/ таким же опросом, и подхватить недописанный JSON ему
        # нельзя. Прежний результат не затираем: по нему приёмник мог уже
        # отработать.
        dst = uniquePath(os.path.join(out_dir, "%s.json" % stem))
        tmp = dst + ".part"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(export, f, ensure_ascii=False, indent=2)
        os.replace(tmp, dst)

        log.info("файл обработан", extra={
            "source_file": name, "pages": len(results),
            "seconds": total, "result": dst})
        return True

    except Exception as e:
        log.error("обработка файла прервана, оригинал уходит в архив",
                  extra={"source_file": name, "error": str(e)}, exc_info=True)
        return False

    finally:
        # Оригинал уезжает в архив в любом случае, включая сбой: иначе
        # демон возьмёт тот же битый файл на следующем обходе и будет
        # падать на нём вечно.
        try:
            shutil.move(src, uniquePath(os.path.join(archive_dir, name)))
        except OSError as e:
            log.error("оригинал не перемещён в архив",
                      extra={"source_file": name, "error": str(e)})


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Демон: забор из in/, результат в out/, оригинал в in/archive/")
    ap.add_argument("--in", dest="in_dir",
                    default=os.path.join(ROOT, "data/in"),
                    help="папка, за которой следим")
    ap.add_argument("--out", dest="out_dir",
                    default=os.path.join(ROOT, "data/out"),
                    help="куда писать JSON результата")
    ap.add_argument("--archive", default="",
                    help="куда убирать обработанные оригиналы "
                         "(по умолчанию <in>/archive)")
    ap.add_argument("--schema", default="",
                    help="имя схемы из config/schemas; без неё — свободный текст")
    ap.add_argument("--language", default="",
                    help="язык рукописи для свободного текста (ru, en, auto)")
    ap.add_argument("--printed", action="store_true",
                    help="свободный текст: читать и печатный текст тоже")
    ap.add_argument("--scale", type=float, default=0.0,
                    help="масштаб рендера PDF (по умолчанию из настроек)")
    ap.add_argument("--poll", type=float, default=POLL_SECONDS,
                    help="пауза между обходами папки, секунды")
    ap.add_argument("--once", action="store_true",
                    help="обработать то, что лежит сейчас, и выйти")
    ap.add_argument("--no-server", action="store_true",
                    help="читать через llama-mtmd-cli (без logprob_min)")
    ap.add_argument("--server-port", type=int, default=8099)
    args = ap.parse_args(argv)

    in_dir = os.path.abspath(args.in_dir)
    out_dir = os.path.abspath(args.out_dir)
    archive_dir = os.path.abspath(args.archive or os.path.join(in_dir, "archive"))

    # Совпадение путей ловим до первого файла, а не после: результат,
    # положенный во вход, демон не возьмёт в работу (json не его формат), но
    # оператор об этом не узнает и будет искать результат не там.
    for name, path in (("--out", out_dir), ("--archive", archive_dir)):
        if path == in_dir:
            log.error("%s совпадает с папкой входа, запуск отменён" % name,
                      extra={"in": in_dir})
            return 2

    import schema as schemas
    sch = None
    if args.schema:
        sch = schemas.get(args.schema)
        if sch is None:
            log.error("схема не найдена, запуск отменён",
                      extra={"schema": args.schema,
                             "available": sorted(schemas.listSchemas()),
                             "errors": schemas.loadErrors()})
            return 2

    for d in (in_dir, out_dir, archive_dir):
        os.makedirs(d, exist_ok=True)

    log.info("демон запускается", extra={
        "in": in_dir, "out": out_dir, "archive": archive_dir,
        "schema": args.schema, "poll_seconds": args.poll, "once": args.once})

    # Имена посторонних файлов, о которых уже сказано в журнале.
    skipped = set()

    # try/finally вокруг создания движка, а не только вокруг цикла: сервер
    # держит видеопамять, и остановить его надо в любом исходе.
    engine = None
    try:
        from ocr import Engine
        engine = Engine()
        if not engine.ready():
            log.error("модель или llama.cpp не найдены, запуск отменён",
                      extra={"model_path": engine.model_path,
                             "llama_path": engine.llama_path})
            return 1
        if not args.no_server:
            engine.startServer(port=args.server_port)
        scale = args.scale or engine.config["ocr"]["pdf_scale"]

        while True:
            try:
                names = sorted(os.listdir(in_dir))
            except OSError as e:
                log.error("папка входа не читается",
                          extra={"in": in_dir, "error": str(e)})
                names = []

            taken = 0
            for name in pickFiles(in_dir, names, skipped):
                processFile(engine, os.path.join(in_dir, name), out_dir,
                            archive_dir, sch=sch,
                            language=args.language or None,
                            printed=args.printed, scale=scale)
                taken += 1

            if args.once:
                log.info("разовый прогон завершён", extra={"files": taken})
                break
            time.sleep(args.poll)

    except KeyboardInterrupt:
        log.info("демон остановлен по Ctrl+C")
    except Exception as e:
        log.error("демон остановлен ошибкой",
                  extra={"error": str(e)}, exc_info=True)
        return 1
    finally:
        if engine is not None:
            engine.stop()

    return 0


if __name__ == "__main__":
    sys.exit(main())
