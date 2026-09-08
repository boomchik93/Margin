# -*- coding: utf-8 -*-
"""
Тесты подъёма и остановки llama-server.

Модель здесь не грузится: проверяется обвязка вокруг процесса. Именно она
тише всего ломает долгий прогон — сервер держит видеопамять, и брошенный
процесс не даст подняться следующему запуску до перезагрузки машины, а
незакрытый файл журнала копится у демона, который живёт неделями.
"""

import os
import sys
import stat
import shutil
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

import llamaserver


def setUpModule():
    # Опрос готовности в бою идёт раз в две секунды: модель грузится долго,
    # и частить незачем. В тестах сервер умирает сразу, и эти паузы —
    # чистое ожидание, поэтому на время тестов опрос учащаем.
    global _REAL_POLL
    _REAL_POLL = llamaserver.STARTUP_POLL
    llamaserver.STARTUP_POLL = 0.05


def tearDownModule():
    llamaserver.STARTUP_POLL = _REAL_POLL


def fakeBinary(directory, script):
    """Исполняемая заглушка вместо llama-server."""
    path = os.path.join(directory, "fake-server")
    with open(path, "w") as f:
        f.write(script)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return path


class StartFailureTest(unittest.TestCase):
    """Сервер, не поднявшийся при старте."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        # Журнал сервера пишется рядом с моделью: уводим его во временную
        # папку, чтобы тест не сорил в рабочий каталог.
        self.model = os.path.join(self.dir, "model.gguf")
        open(self.model, "wb").close()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def server(self, script):
        return llamaserver.LlamaServer(
            fakeBinary(self.dir, script), self.model,
            os.path.join(self.dir, "mmproj.gguf"), "99", 4096,
            port=18099, threads=1)

    def test_умершийПриСтартеНеОставляетОткрытыйЖурнал(self):
        # Демон живёт неделями и сервер может подниматься не один раз:
        # незакрытый дескриптор копится с каждой неудачей.
        s = self.server("#!/bin/sh\nexit 1\n")
        self.assertFalse(s.start())
        self.assertIsNone(s._log_file)

    def test_умершийПриСтартеНеОставляетСсылкуНаПроцесс(self):
        s = self.server("#!/bin/sh\nexit 1\n")
        self.assertFalse(s.start())
        self.assertIsNone(s.proc)

    def test_ненайденныйБинарникНеПадает(self):
        s = llamaserver.LlamaServer(
            os.path.join(self.dir, "нет-такого"), self.model,
            os.path.join(self.dir, "mmproj.gguf"), "99", 4096,
            port=18099, threads=1)
        self.assertFalse(s.start())
        self.assertIsNone(s._log_file)

    def test_stopПослеНеудачиНеПадает(self):
        # main() зовёт stop() в finally независимо от того, поднялся ли сервер.
        s = self.server("#!/bin/sh\nexit 1\n")
        s.start()
        s.stop()
        s.stop()

    def test_контекстныйМенеджерСообщаетОНеудаче(self):
        s = self.server("#!/bin/sh\nexit 1\n")
        with self.assertRaises(RuntimeError):
            with s:
                pass


class StopTest(unittest.TestCase):
    """Остановка сервера."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.model = os.path.join(self.dir, "model.gguf")
        open(self.model, "wb").close()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_stopБезЗапускаНеПадает(self):
        s = llamaserver.LlamaServer(
            os.path.join(self.dir, "fake"), self.model,
            os.path.join(self.dir, "mmproj.gguf"), "99", 4096,
            port=18099, threads=1)
        s.stop()
        self.assertIsNone(s.proc)


class FindServerTest(unittest.TestCase):
    """Поиск бинарника рядом с llama-mtmd-cli."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_находитРядомСCli(self):
        cli = os.path.join(self.dir, "llama-mtmd-cli")
        open(cli, "wb").close()
        server = os.path.join(self.dir, "llama-server")
        open(server, "wb").close()
        os.chmod(server, os.stat(server).st_mode | stat.S_IEXEC)
        self.assertEqual(llamaserver.findServer(cli), server)

    def test_отсутствующийОтдаётNone(self):
        cli = os.path.join(self.dir, "llama-mtmd-cli")
        open(cli, "wb").close()
        self.assertIsNone(llamaserver.findServer(cli))


class ReadTest(unittest.TestCase):
    """Запрос чтения: вместо llama-server — маленький HTTP-сервер в потоке."""

    def setUp(self):
        import http.server
        import json
        import threading

        test = self
        test.requests = []
        test.reply = {"choices": [{
            "message": {"content": '{"lines": ["ok"]}'},
            "logprobs": {"content": [{"token": "ok", "logprob": -0.2}]},
            "finish_reason": "stop"}]}
        test.status = 200

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                test.requests.append(
                    (self.path, json.loads(self.rfile.read(length))))
                body = json.dumps(test.reply).encode("utf-8")
                self.send_response(test.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

        self.dir = tempfile.mkdtemp()
        self.image = os.path.join(self.dir, "page.png")
        with open(self.image, "wb") as f:
            f.write(b"\x89PNG-bytes")
        self.server = llamaserver.LlamaServer(
            "unused", os.path.join(self.dir, "model.gguf"),
            os.path.join(self.dir, "mmproj.gguf"), "99", 4096,
            port=self.httpd.server_address[1])

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_текстИВероятностиВозвращаются(self):
        text, tokens = self.server.read("prompt", self.image)
        self.assertEqual(text, '{"lines": ["ok"]}')
        self.assertEqual(tokens, [{"token": "ok", "logprob": -0.2}])

    def test_запросФиксируетВыборкуИПроситВероятности(self):
        # Одной temperature=0 для воспроизводимости мало: сервер подставляет
        # свои top_k/top_p и случайный seed.
        self.server.read("prompt", self.image, n_predict=512)
        path, body = self.requests[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual((body["temperature"], body["top_k"], body["seed"]),
                         (0, 1, 0))
        self.assertEqual(body["max_tokens"], 512)
        self.assertTrue(body["logprobs"])
        content = body["messages"][0]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "prompt"})
        self.assertTrue(content[1]["image_url"]["url"].startswith(
            "data:image/png;base64,"))

    def test_пустойОтветЭтоОтказ(self):
        # Так проявляется переполнение контекста: без проверки документ
        # выглядел бы незаполненным.
        self.reply = {"choices": [{"message": {"content": "  "},
                                   "finish_reason": "length"}]}
        self.assertEqual(self.server.read("prompt", self.image), ("", []))

    def test_ошибкаСервераНеБросаетИсключение(self):
        self.status = 500
        self.assertEqual(self.server.read("prompt", self.image), ("", []))

    def test_непрочитаннаяКартинкаНеБросаетИсключение(self):
        self.assertEqual(
            self.server.read("prompt", os.path.join(self.dir, "missing.png")),
            ("", []))
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
