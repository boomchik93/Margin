# -*- coding: utf-8 -*-
"""
Тесты журналирования.

Проверяется не «логгер что-то написал», а свойства, на которые опирается
разбор инцидента: строка читается машиной, идентификатор запроса не теряется,
аудит переживает несериализуемые значения, а недоступная директория логов не
роняет сервис.
"""

import json
import logging
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))


def _closeHandlers():
    """Закрыть файловые обработчики перед удалением временной директории.

    Без явного закрытия открытые файлы переживают cleanup(), и тест сообщает
    о ResourceWarning вместо результата проверки.
    """
    for logger in (logging.getLogger(), logging.getLogger("ocr.audit")):
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)
    # Аудит-логгер кэшируется в модуле; сбрасываем, чтобы следующий тест
    # завёл его заново в своей директории.
    if "logging_setup" in sys.modules:
        sys.modules["logging_setup"]._audit_logger = None


class JsonFormatTest(unittest.TestCase):
    """Каждая запись app.log — самостоятельный JSON-объект в одной строке."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LOG_DIR"] = self.tmp.name
        # Модуль читает переменные окружения на импорте, поэтому для теста
        # его нужно перечитать заново.
        for name in list(sys.modules):
            if name == "logging_setup":
                del sys.modules[name]
        import logging_setup
        self.ls = logging_setup
        self.ls.LOG_DIR = self.tmp.name
        self.ls.setup(force=True)

    def tearDown(self):
        _closeHandlers()
        self.tmp.cleanup()
        os.environ.pop("LOG_DIR", None)

    def _lines(self):
        path = os.path.join(self.tmp.name, "app.log")
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_записьРазбираетсяКакJson(self):
        self.ls.getLogger("test").info("проверка", extra={"field": "city"})
        entries = self._lines()
        self.assertTrue(entries)
        last = entries[-1]
        self.assertEqual(last["event"], "проверка")
        self.assertEqual(last["field"], "city")
        self.assertEqual(last["level"], "INFO")
        self.assertIn("ts", last)

    def test_идентификаторЗапросаПопадаетВЗаписьБезЯвнойПередачи(self):
        """Идентификатор ставится один раз на входе в запрос и должен
        доходить до записей из глубины пайплайна сам."""
        self.ls.setRequestId("abc12345")
        self.ls.getLogger("deep.module").warning("что-то не так")
        self.ls.setRequestId("")

        entry = self._lines()[-1]
        self.assertEqual(entry["request_id"], "abc12345")

    def test_несериализуемоеЗначениеНеРоняетЗапись(self):
        """Логгер обязан пережить любой объект в extra: упавшее логирование
        уносит с собой запись об аварии, ради которой оно и нужно."""
        class Opaque:
            def __repr__(self):
                return "<opaque>"

        self.ls.getLogger("test").info("объект", extra={"obj": Opaque()})
        entry = self._lines()[-1]
        self.assertEqual(entry["obj"], "<opaque>")

    def test_исключениеПишетсяСоСтектрейсом(self):
        log = self.ls.getLogger("test")
        try:
            raise ValueError("тестовая ошибка")
        except ValueError:
            log.error("упало", exc_info=True)

        entry = self._lines()[-1]
        self.assertIn("ValueError: тестовая ошибка", entry["exception"])
        self.assertIn("Traceback", entry["exception"])

    def test_ошибкиДублируютсяВОтдельныйФайл(self):
        """errors.log нужен, чтобы в момент аварии не грепать сотни мегабайт
        общего журнала."""
        self.ls.getLogger("test").info("обычное событие")
        self.ls.getLogger("test").error("авария")

        with open(os.path.join(self.tmp.name, "errors.log"), encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn("авария", content)
        self.assertNotIn("обычное событие", content)


class AuditTest(unittest.TestCase):
    """Аудит-лог: по записи на страницу, отдельно от отладочного журнала."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        for name in list(sys.modules):
            if name == "logging_setup":
                del sys.modules[name]
        os.environ["LOG_DIR"] = self.tmp.name
        import logging_setup
        self.ls = logging_setup
        self.ls.LOG_DIR = self.tmp.name
        self.ls.setup(force=True)

    def tearDown(self):
        _closeHandlers()
        self.tmp.cleanup()
        os.environ.pop("LOG_DIR", None)

    def _audit(self):
        path = os.path.join(self.tmp.name, "audit.jsonl")
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_записьСодержитПереданныеПоля(self):
        self.ls.setRequestId("req00001")
        self.ls.audit({"page": 3, "fields": {"city": "КАЗАНЬ"},
                       "needs_review": ["date"]})
        self.ls.setRequestId("")

        entry = self._audit()[-1]
        self.assertEqual(entry["page"], 3)
        self.assertEqual(entry["fields"]["city"], "КАЗАНЬ")
        self.assertEqual(entry["needs_review"], ["date"])
        self.assertEqual(entry["request_id"], "req00001")
        self.assertIn("ts", entry)

    def test_несериализуемаяЗаписьНеТеряетсяМолча(self):
        """Отсутствие записи о странице читается как «страницу не присылали».
        Поэтому при сбое сериализации пишется хотя бы отметка об ошибке."""
        class Opaque:
            pass

        self.ls.audit({"page": 1, "bad": {Opaque(): "ключ-объект"}})
        entry = self._audit()[-1]
        self.assertIn("audit_error", entry)

    def test_аудитНеПопадаетВОбщийЖурнал(self):
        """У аудита свой срок хранения и свой читатель; смешение сделало бы
        app.log нечитаемым."""
        self.ls.audit({"page": 1, "fields": {}})
        with open(os.path.join(self.tmp.name, "app.log"), encoding="utf-8") as fh:
            app_log = fh.read()
        self.assertNotIn('"page": 1', app_log)
