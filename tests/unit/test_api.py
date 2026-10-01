# -*- coding: utf-8 -*-
"""
Тесты HTTP-слоя.

Модель здесь не запускается: движок подменяется заглушкой. Проверяется то,
что видит клиент, — коды ответов, идентификатор запроса, форма ответа в обоих
режимах и служебные ручки, по которым проверяют состояние сервиса.
"""
import io
import json
import os
import shutil
import unittest

import support
import app as app_module
import storage
from ocr import HAS_FITZ

client = None


def setUpModule():
    global client, _uploads, _saved_uploads
    app_module.app.config["TESTING"] = True
    # Загрузки идут во временную папку, а не в рабочий каталог проекта.
    _uploads = support.tmpDir()
    _saved_uploads = app_module.app.config["UPLOAD_FOLDER"]
    app_module.app.config["UPLOAD_FOLDER"] = _uploads
    client = app_module.app.test_client()


def tearDownModule():
    app_module.app.config["UPLOAD_FOLDER"] = _saved_uploads
    shutil.rmtree(_uploads, ignore_errors=True)


def png():
    path = os.path.join(_uploads, "sample-src.png")
    support.makeImage(path)
    with open(path, "rb") as f:
        data = f.read()
    os.remove(path)
    return io.BytesIO(data)


def post(filename="scan.png", body=None, **form):
    data = {"file": (body or png(), filename)}
    data.update(form)
    return client.post("/api/ocr", data=data,
                       content_type="multipart/form-data")


class FakeEngine:
    """Подмена движка: отдаёт заданный результат и запоминает вызовы."""

    def __init__(self, result=None):
        self.calls = []
        self.result = result

    def install(self, test):
        engine = app_module.engine
        saved = (engine.readPage, engine.modelExists, engine.llamaExists)
        engine.readPage = self.readPage
        engine.modelExists = lambda: True
        engine.llamaExists = lambda: True

        def restore():
            engine.readPage, engine.modelExists, engine.llamaExists = saved
        test.addCleanup(restore)
        return self

    def readPage(self, image_path, sch=None, language=None, printed=False):
        self.calls.append({"schema": sch["name"] if sch else None,
                           "language": language, "printed": printed,
                           "exists": os.path.exists(image_path)})
        if self.result is not None:
            return dict(self.result)
        if sch is None:
            return {"mode": "text", "parsed": True, "text": "первая\nвторая",
                    "lines": ["первая", "вторая"], "unclear_count": 0,
                    "timings": {}, "backend": "cli", "model": "m.gguf"}
        return {"mode": "form", "schema": sch["name"], "parsed": True,
                "fields": {f["key"]: "" for f in sch["fields"]},
                "confidence": {}, "confidence_reason": {}, "dict_status": {},
                "candidates": {}, "needs_review": ["city"],
                "trace": {"city": {"recognized": "x"}}, "dict_version": "v1",
                "timings": {}, "backend": "cli", "model": "m.gguf"}


class ServiceTest(unittest.TestCase):
    def test_проверкаЖивостиОтвечаетДажеБезМодели(self):
        """Готовность к распознаванию сюда не входит намеренно: иначе Docker
        перезапускал бы контейнер из-за отсутствующего файла модели."""
        response = client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "ok")

    def test_идентификаторЗапросаВозвращаетсяВЗаголовкеИТеле(self):
        response = client.get("/api/health")
        request_id = response.headers.get("X-Request-ID")
        self.assertTrue(request_id)
        self.assertEqual(response.get_json()["request_id"], request_id)

    def test_идентификаторКлиентаПодхватывается(self):
        response = client.get("/api/health",
                              headers={"X-Request-ID": "client-2026-0042"})
        self.assertEqual(response.headers["X-Request-ID"], "client-2026-0042")

    def test_небезопасныйИдентификаторОчищается(self):
        """Идентификатор попадает в имена файлов сырого вывода."""
        response = client.get(
            "/api/health", headers={"X-Request-ID": "../../etc/passwd"})
        self.assertNotIn("/", response.headers["X-Request-ID"])

    def test_статусПоказываетГотовностьИСостав(self):
        payload = client.get("/api/status").get_json()
        for key in ("ready", "model_exists", "llama_exists", "backend",
                    "schemas", "dictionaries", "storage", "hardware"):
            self.assertIn(key, payload)

    def test_схемаПримерВидна(self):
        payload = client.get("/api/schemas").get_json()
        names = [s["name"] for s in payload["schemas"]]
        self.assertIn("example_form", names)

        detail = client.get("/api/schemas/example_form").get_json()
        self.assertTrue(detail["success"])
        self.assertIn("prompt", detail)
        self.assertEqual(client.get("/api/schemas/nope").status_code, 404)

    def test_словариИПерезагрузка(self):
        payload = client.get("/api/dictionaries").get_json()
        for key in ("dir", "dictionaries", "errors", "version"):
            self.assertIn(key, payload)
        response = client.post("/api/dictionaries/reload")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["success"])

    def test_несуществующийПутьВозвращаетJson(self):
        """Клиент разбирает ответы как JSON; HTML-страница ошибки сломала бы
        разбор на его стороне."""
        response = client.get("/api/нет-такого")
        self.assertEqual(response.status_code, 404)
        self.assertIn("request_id", response.get_json())

    def test_внутренняяОшибкаНеРаскрываетДеталей(self):
        """В тексте исключения бывают пути и куски распознанных данных;
        наружу уходит только идентификатор запроса."""
        error = RuntimeError("секретный путь /srv/данные/файл.pdf")
        with app_module.app.test_request_context("/api/ocr"):
            response, code = app_module.handleUnexpected(error)
            body = json.dumps(response.get_json(), ensure_ascii=False)
        self.assertEqual(code, 500)
        self.assertNotIn("секретный", body)
        self.assertNotIn("/srv/", body)

    def test_вебИнтерфейсОткрывается(self):
        response = client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("<h1>Margin</h1>",
                      response.get_data(as_text=True))


class UploadValidationTest(unittest.TestCase):
    """Отказы приёма файла: код и текст ошибки видит клиент."""

    def test_запросБезФайла(self):
        response = client.post("/api/ocr")
        self.assertEqual(response.status_code, 400)
        body = response.get_json()
        self.assertFalse(body["success"])
        self.assertIn("request_id", body)

    def test_пустоеИмяФайла(self):
        self.assertEqual(post(filename="").status_code, 400)

    def test_неподдерживаемыйФормат(self):
        response = post(filename="program.exe", body=io.BytesIO(b"MZ"))
        self.assertEqual(response.status_code, 400)
        self.assertIn("формат", response.get_json()["error"])

    def test_неизвестнаяСхема(self):
        response = post(schema="нет-такой")
        self.assertEqual(response.status_code, 400)
        self.assertIn("схема", response.get_json()["error"])

    def test_битаяСхемаВЗапросеНазываетПричину(self):
        response = post(schema_json='{"fields": [{"key": "Плохой ключ"}]}')
        self.assertEqual(response.status_code, 400)
        self.assertIn("key", response.get_json()["error"])
        self.assertEqual(post(schema_json="{не json").status_code, 400)

    def test_отсутствиеМоделиДаёт503СПонятнойПричиной(self):
        """Сервис поднимается без модели (так удобнее разворачивать), но
        запрос распознавания обязан сказать, чего не хватает."""
        if app_module.engine.ready():
            self.skipTest("модель установлена, отказ не воспроизводится")
        response = post()
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.get_json()["success"])


class RecognitionTest(unittest.TestCase):
    def test_безСхемыВозвращаетсяТекст(self):
        engine = FakeEngine().install(self)
        response = post()
        body = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(body["success"])
        self.assertEqual((body["type"], body["mode"]), ("image", "text"))
        self.assertEqual(body["lines"], ["первая", "вторая"])
        self.assertEqual(engine.calls[0]["schema"], None)
        self.assertTrue(engine.calls[0]["exists"])

    def test_соСхемойВозвращаютсяПоля(self):
        engine = FakeEngine().install(self)
        body = post(schema="example_form").get_json()
        self.assertEqual(body["mode"], "form")
        self.assertEqual(body["schema"], "example_form")
        self.assertIn("full_name", body["fields"])
        self.assertEqual(body["needs_review"], ["city"])
        # Трассировка стадий наружу не идёт: она лежит в истории.
        self.assertNotIn("trace", body)
        self.assertEqual(engine.calls[0]["schema"], "example_form")

    def test_схемаМожетПрийтиВЗапросе(self):
        engine = FakeEngine().install(self)
        body = post(schema_json=json.dumps(
            {"fields": [{"key": "note", "label": "Заметка"}]})).get_json()
        self.assertEqual(body["schema"], "inline")
        self.assertEqual(list(body["fields"]), ["note"])
        self.assertEqual(engine.calls[0]["schema"], "inline")

    def test_параметрыТекстовогоРежимаДоходятДоДвижка(self):
        engine = FakeEngine().install(self)
        post(language="EN", printed="1")
        self.assertEqual(engine.calls[0]["language"], "en")
        self.assertTrue(engine.calls[0]["printed"])

    def test_имяФайлаНаКириллицеПринимается(self):
        FakeEngine().install(self)
        self.assertEqual(post(filename="письмо.png").status_code, 200)

    def test_загруженныйФайлУдаляетсяПослеОбработки(self):
        FakeEngine().install(self)
        post()
        self.assertEqual(os.listdir(_uploads), [])

    def test_молчаниеМоделиДаёт502(self):
        FakeEngine({"mode": "text", "parsed": False, "text": "", "lines": [],
                    "unclear_count": 0, "error": "model_no_response"}
                   ).install(self)
        response = post()
        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.get_json()["success"])

    def test_результатПопадаетВИсторию(self):
        if not storage.ready():
            self.skipTest("хранилище недоступно")
        FakeEngine().install(self)
        data = {"file": (png(), "scan.png"), "schema": "example_form"}
        response = client.post("/api/ocr", data=data,
                               content_type="multipart/form-data",
                               headers={"X-Request-ID": "history-test-1"})
        self.assertEqual(response.status_code, 200)

        detail = client.get("/api/results/history-test-1").get_json()
        self.assertTrue(detail["success"])
        page = detail["results"][0]
        self.assertEqual(page["schema_name"], "example_form")
        self.assertEqual(page["needs_review"], ["city"])
        self.assertEqual(page["trace"], {"city": {"recognized": "x"}})

        listed = client.get("/api/results?schema=example_form&needs_review=1")
        self.assertIn("history-test-1",
                      [i["request_id"] for i in listed.get_json()["items"]])

    @unittest.skipUnless(HAS_FITZ, "PyMuPDF не установлен")
    def test_pdfОбрабатываетсяПостранично(self):
        import fitz
        doc = fitz.open()
        for _ in range(3):
            doc.new_page()
        pdf = io.BytesIO(doc.tobytes())
        doc.close()

        engine = FakeEngine().install(self)
        body = post(filename="scan.pdf", body=pdf).get_json()
        self.assertEqual((body["type"], body["pages"]), ("pdf", 3))
        self.assertEqual([r["page"] for r in body["results"]], [1, 2, 3])
        self.assertEqual(len(engine.calls), 3)


class ResultsApiTest(unittest.TestCase):
    def test_историяОтвечаетДажеПустая(self):
        response = client.get("/api/results")
        self.assertEqual(response.status_code, 200)
        self.assertIn("items", response.get_json())

    def test_некорректныйLimitОтклоняется(self):
        self.assertEqual(client.get("/api/results?limit=много").status_code, 400)

    def test_несуществующийЗапрос404(self):
        self.assertEqual(client.get("/api/results/нет-такого").status_code, 404)


class RateLimitTest(unittest.TestCase):
    def test_превышениеЛимитаДаёт429(self):
        FakeEngine().install(self)
        limit = app_module.config["api"]["rate_limit"]
        saved = dict(limit)
        self.addCleanup(lambda: (limit.update(saved),
                                 app_module.request_counts.clear()))
        limit.update({"enabled": True, "requests_per_minute": 2})
        app_module.request_counts.clear()

        self.assertEqual(post().status_code, 200)
        self.assertEqual(post().status_code, 200)
        self.assertEqual(post().status_code, 429)


if __name__ == "__main__":
    unittest.main()
