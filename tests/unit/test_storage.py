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


class ListTest(StorageTestCase):
    def setUp(self):
        super().setUp()
        storage.save(self.record(request_id="a", created_at="2026-01-10T10:00:00"))
        storage.save(self.record(
            request_id="b", created_at="2026-02-10T10:00:00", mode="text",
            schema_name="", fields={}, confidence={},
            text="Купить хлеб и молоко", needs_review=["text"]))
        storage.save(self.record(
            request_id="c", created_at="2026-03-10T10:00:00",
            status="error", error="recognition_failed"))

    def ids(self, **filters):
        return [i["request_id"] for i in storage.listResults(**filters)["items"]]

    def test_новыеПервыми(self):
        self.assertEqual(self.ids(), ["c", "b", "a"])

    def test_границыДатВключительны(self):
        self.assertEqual(self.ids(date_from="2026-02-10",
                                  date_to="2026-02-10"), ["b"])

    def test_поискПоТекстуИПоЗначениямПолей(self):
        self.assertEqual(self.ids(query="хлеб"), ["b"])
        self.assertEqual(self.ids(query="Казань"), ["c", "a"])

    def test_фильтрПоСхемеИСтатусу(self):
        self.assertEqual(self.ids(schema_name="sample"), ["c", "a"])
        self.assertEqual(self.ids(status="error"), ["c"])

    def test_толькоТребующиеПроверки(self):
        self.assertEqual(self.ids(needs_review_only=True), ["b"])

    def test_постраничнаяВыдачаСчитаетВсе(self):
        page = storage.listResults(limit=2, offset=0)
        self.assertEqual(page["total"], 3)
        self.assertEqual(len(page["items"]), 2)

    def test_списокОтдаётНачалоТекстаАНеВесьТекст(self):
        storage.save(self.record(request_id="d", mode="text", text="я" * 5000))
        item = storage.listResults(limit=1)["items"][0]
        self.assertEqual(len(item["preview"]), 200)

    def test_сводка(self):
        stats = storage.stats()
        self.assertTrue(stats["ready"])
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["needs_review"], 1)
        self.assertEqual(stats["failed"], 1)


class FailureTest(unittest.TestCase):
    """Отказ хранилища не роняет сервис."""

    def setUp(self):
        self.saved = (storage.DB_PATH, storage.ENABLED, storage._ready)

    def tearDown(self):
        storage.DB_PATH, storage.ENABLED, storage._ready = self.saved

    def test_недоступныйПутьНеБросаетИсключение(self):
        storage.DB_PATH = "/dev/null/нет/results.db"
        storage.ENABLED = True
        self.assertFalse(storage.init())
        self.assertFalse(storage.ready())
        self.assertIsNone(storage.save({"request_id": "x"}))
        self.assertEqual(storage.listResults()["items"], [])
        self.assertEqual(storage.getByRequest("x"), [])
        self.assertFalse(storage.stats()["ready"])

    def test_выключенноеХранилищеНеСоздаётФайл(self):
        tmp = support.tmpDir()
        try:
            storage.DB_PATH = os.path.join(tmp, "results.db")
            storage.ENABLED = False
            storage._ready = False
            self.assertFalse(storage.init())
            self.assertIsNone(storage.save({"request_id": "x"}))
            self.assertFalse(os.path.exists(storage.DB_PATH))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
