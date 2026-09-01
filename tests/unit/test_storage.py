# -*- coding: utf-8 -*-
"""
Тесты хранилища результатов.

История нужна, чтобы через месяц ответить, что сервис вернул по конкретному
файлу. Проверяется, что запись находится теми способами, которыми её будут
искать, и что отказ хранилища не превращается в отказ распознавания.
"""
import os
import shutil
import unittest

import support
import storage


class StorageTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = support.tmpDir()
        self.saved = (storage.DB_PATH, storage.ENABLED, storage._ready)
        storage.DB_PATH = os.path.join(self.dir, "results.db")
        storage.ENABLED = True
        self.assertTrue(storage.init())

    def tearDown(self):
        storage.DB_PATH, storage.ENABLED, storage._ready = self.saved
        shutil.rmtree(self.dir, ignore_errors=True)

    def record(self, **extra):
        base = {
            "request_id": "req-1", "source_name": "scan.png",
            "source_type": "image", "page": 1, "pages_total": 1,
            "status": "ok", "duration_seconds": 1.5, "mode": "form",
            "schema_name": "sample",
            "fields": {"city": "Казань", "title": "ОТЧЁТ"},
            "confidence": {"city": 55, "title": 55, "note": 0},
            "needs_review": [], "model_name": "model.gguf",
        }
        base.update(extra)
        return base


class SaveTest(StorageTestCase):
    def test_записьНаходитсяПоИдентификаторуЗапроса(self):
        self.assertIsNotNone(storage.save(self.record()))
        items = storage.getByRequest("req-1")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["fields"]["city"], "Казань")
        self.assertEqual(items[0]["needs_review"], [])

    def test_страницыОдногоЗапросаИдутПоПорядку(self):
        for page in (3, 1, 2):
            storage.save(self.record(page=page, pages_total=3))
        self.assertEqual([r["page"] for r in storage.getByRequest("req-1")],
                         [1, 2, 3])

    def test_минимальнаяУверенностьНеСчитаетПустыеПоля(self):
        storage.save(self.record(
            confidence={"city": 55, "issued": 25, "note": 0}))
        item = storage.listResults()["items"][0]
        self.assertEqual(item["min_confidence"], 25)

    def test_несуществующийЗапросДаётПустойСписок(self):
        self.assertEqual(storage.getByRequest("нет"), [])
