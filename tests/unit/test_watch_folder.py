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
