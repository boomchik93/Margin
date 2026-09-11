# -*- coding: utf-8 -*-
"""
Тесты папочной обвязки.

Демон живёт неделями без присмотра, и его ошибки тихие: файл взят
недописанным, результат затёр прежний, битый файл берётся в работу снова и
снова. Проверяется именно это; модель подменена заглушкой.
"""
import json
import os
import shutil
import unittest

import support
import watch_folder


class FakeEngine:
    def __init__(self, fail_on=()):
        self.fail_on = set(fail_on)
        self.calls = []

    def readPage(self, image_path, sch=None, language=None, printed=False):
        self.calls.append(os.path.basename(image_path))
        if os.path.basename(image_path) in self.fail_on:
            raise RuntimeError("модель упала")
        return {"mode": "text", "parsed": True, "text": "строка",
                "lines": ["строка"], "unclear_count": 0,
                "trace": {"служебное": 1}}


class FolderTestCase(unittest.TestCase):
    def setUp(self):
        self.root = support.tmpDir()
        self.in_dir = os.path.join(self.root, "in")
        self.out_dir = os.path.join(self.root, "out")
        self.archive = os.path.join(self.in_dir, "archive")
        for d in (self.in_dir, self.out_dir, self.archive):
            os.makedirs(d)
        # Выдержка «файл дописан» в тестах не нужна: файлы пишутся целиком.
        self.saved_settle = watch_folder.SETTLE_SECONDS
        watch_folder.SETTLE_SECONDS = 0.0

    def tearDown(self):
        watch_folder.SETTLE_SECONDS = self.saved_settle
        shutil.rmtree(self.root, ignore_errors=True)

    def put(self, name, content=b"data"):
        path = os.path.join(self.in_dir, name)
        with open(path, "wb") as f:
            f.write(content)
        return path


class PickFilesTest(FolderTestCase):
    def pick(self, skipped=None):
        skipped = set() if skipped is None else skipped
        return watch_folder.pickFiles(
            self.in_dir, sorted(os.listdir(self.in_dir)), skipped)

    def test_берутсяТолькоПоддерживаемыеФорматы(self):
        self.put("a.png")
        self.put("b.PDF")
        self.put("notes.txt")
        self.assertEqual(self.pick(), ["a.png", "b.PDF"])

    def test_скрытыеФайлыИПапкиПропускаются(self):
        self.put(".hidden.png")
        self.put("a.png")
        self.assertEqual(self.pick(), ["a.png"])

    def test_пустойФайлСчитаетсяНедописанным(self):
        self.put("empty.png", b"")
        self.assertEqual(self.pick(), [])

    def test_оПостороннемФайлеГоворитсяОдинРаз(self):
        """Обход идёт раз в секунду: жалоба на каждом обходе утопила бы
        журнал в одной и той же строке."""
        self.put("notes.txt")
        skipped = set()
        self.pick(skipped)
        self.assertEqual(skipped, {"notes.txt"})
        os.remove(os.path.join(self.in_dir, "notes.txt"))
        self.pick(skipped)
        self.assertEqual(skipped, set())

    def test_растущийФайлНеБерётся(self):
        path = self.put("a.png")
        real_sleep = watch_folder.time.sleep

        def grow(_):
            with open(path, "ab") as f:
                f.write(b"more")
        watch_folder.time.sleep = grow
        try:
            self.assertEqual(watch_folder.stableFiles([path], settle=0.01), [])
        finally:
            watch_folder.time.sleep = real_sleep


class UniquePathTest(FolderTestCase):
    def test_свободноеИмяНеМеняется(self):
        path = os.path.join(self.out_dir, "a.json")
        self.assertEqual(watch_folder.uniquePath(path), path)

    def test_занятоеИмяПолучаетМетку(self):
        path = os.path.join(self.out_dir, "a.json")
        open(path, "w").close()
        second = watch_folder.uniquePath(path)
        self.assertNotEqual(second, path)
        self.assertTrue(second.endswith(".json"))
        open(second, "w").close()
        self.assertNotIn(watch_folder.uniquePath(path), (path, second))


class ProcessFileTest(FolderTestCase):
    def run_file(self, name, engine=None, **kwargs):
        engine = engine or FakeEngine()
        ok = watch_folder.processFile(engine, os.path.join(self.in_dir, name),
                                      self.out_dir, self.archive, **kwargs)
        return ok, engine

    def result(self, name):
        with open(os.path.join(self.out_dir, name), encoding="utf-8") as f:
            return json.load(f)

    def test_результатВOutОригиналВАрхив(self):
        self.put("letter.png")
        ok, _ = self.run_file("letter.png")
        self.assertTrue(ok)
        export = self.result("letter.json")
        self.assertEqual(export["source_file"], "letter.png")
        self.assertEqual(export["pages"], 1)
        self.assertEqual(export["results"][0]["text"], "строка")
        self.assertEqual(os.listdir(self.in_dir), ["archive"])
        self.assertEqual(os.listdir(self.archive), ["letter.png"])

    def test_трассировкаВРезультатНеПопадает(self):
        self.put("letter.png")
        self.run_file("letter.png")
        self.assertNotIn("trace", self.result("letter.json")["results"][0])

    def test_недописанногоФайлаВOutНеОстаётся(self):
        """Приёмник читает out/ таким же опросом и подхватить половину JSON
        ему нельзя: запись идёт через временный файл."""
        self.put("letter.png")
        self.run_file("letter.png")
        self.assertEqual(os.listdir(self.out_dir), ["letter.json"])

    def test_повторныйЗаездНеЗатираетПрежнийРезультат(self):
        self.put("letter.png")
        self.run_file("letter.png")
        self.put("letter.png")
        self.run_file("letter.png")
        self.assertEqual(len(os.listdir(self.out_dir)), 2)
        self.assertEqual(len(os.listdir(self.archive)), 2)

    def test_сбойСтраницыНеТеряетФайл(self):
        self.put("bad.png")
        ok, _ = self.run_file("bad.png", FakeEngine(fail_on=["bad.png"]))
        self.assertTrue(ok)
        export = self.result("bad.json")
        self.assertEqual(export["pages_failed"], 1)
        self.assertEqual(export["results"][0]["error"], "page_failed")

    def test_битыйФайлУезжаетВАрхивИНеБерётсяСнова(self):
        """Иначе демон падал бы на нём на каждом обходе вечно."""
        self.put("broken.pdf", b"not a pdf")
        ok, _ = self.run_file("broken.pdf")
        self.assertFalse(ok)
        self.assertEqual(os.listdir(self.out_dir), [])
        self.assertEqual(os.listdir(self.archive), ["broken.pdf"])
        self.assertEqual(os.listdir(self.in_dir), ["archive"])


class MainTest(FolderTestCase):
    def test_выходСовпадающийСВходомОтклоняется(self):
        code = watch_folder.main(["--in", self.in_dir, "--out", self.in_dir,
                                  "--once"])
        self.assertEqual(code, 2)

    def test_неизвестнаяСхемаОтклоняется(self):
        code = watch_folder.main(["--in", self.in_dir, "--out", self.out_dir,
                                  "--schema", "нет-такой", "--once"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
