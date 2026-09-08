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
