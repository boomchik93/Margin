# -*- coding: utf-8 -*-
"""Чтение изображения через llama-server вместо llama-mtmd-cli.

Зачем. CLI печатает в stdout только текст: вероятностей токенов у него нет,
и на этом пути `logprob_min` остаётся пустым. Модель считает уверенность по
каждому токену в любом случае — иначе не выбрала бы, какую букву написать, —
но CLI эту величину выбрасывает.

Сервер той же сборки отдаёт её наружу и, кроме того, держит модель в памяти
между запросами: CLI грузит её заново на каждый проход.

Используется `/v1/chat/completions`, а не `/completion`: chat-эндпоинт
применяет шаблон чата модели. Через сырой `/completion` Qwen2.5-VL повторяет
промпт эхом вместо ответа.
"""
import os
import json
import time
import base64
import subprocess
import socket
import urllib.error
import urllib.request

from logging_setup import getLogger

log = getLogger("llamaserver")

# Сколько ждать поднятия сервера: загрузка модели и проектора с диска в
# видеопамять занимает десятки секунд, на холодном кэше дольше.
STARTUP_TIMEOUT = 300
STARTUP_POLL = 2.0

# Предел ожидания одного прохода. В конфиге таймаут может быть null ("ждать
# сколько понадобится") — для запуска процесса это приемлемо, он рано или
# поздно завершится сам. HTTP-запрос так не работает: сервер отвечает "ok" на
# /health, а конкретный запрос висит без ответа, и очередь встаёт целиком.
REQUEST_TIMEOUT = 600

# Минимальное разрешение картинки в токенах. Сервер сам предупреждает при
# загрузке: "Qwen-VL models require at minimum 1024 image tokens to function
# correctly". Без этого страница ужимается, мелкие рукописные цифры теряются,
# и длинные номера читаются как повтор одной цифры.
IMAGE_MIN_TOKENS = 1024

# Один слот, а не четыре по умолчанию. При нескольких слотах сервер
# переиспользует общий префикс контекста между запросами (в логе это видно
# как "selected slot by LCP similarity"), и модель дотягивает в ответ
# значения ПРЕДЫДУЩЕГО документа: пустое поле заполняется тем, что стояло в
# нём страницей раньше.
#
# Страницы всё равно обрабатываются по одной, поэтому параллельные слоты
# ничего не ускоряют — они только создают этот перенос между документами.
PARALLEL_SLOTS = 1

# Контекст сервера. У постоянного сервера KV-кэш резервируется под весь
# контекст сразу, и при нехватке видеопамяти вытесняет кэш промптов: в логе
# это "making room for prompt cache entry", на практике — кратное замедление.
#
# Плотная страница даёт около 5300 токенов промпта вместе с картинкой, к ним
# добавляется до 2000-4000 токенов ответа. При контексте 8192 запас исчезал,
# и страница изредка возвращалась пустой за пару секунд: запрос не влезал
# целиком. 12288 оставляет запас и всё ещё меньше 16384 по умолчанию, то есть
# место под кэш промптов остаётся.
SERVER_CONTEXT = 12288


class LlamaServer:
    """Запущенный на время работы llama-server.

    Держится один процесс: загрузка модели занимает секунды, и платить их на
    каждой странице (а тем более на каждом зонном проходе) незачем.
    """

    def __init__(self, binary, model_path, mmproj_path, gpu_layers,
                 context_length, host="127.0.0.1", port=8099, threads=None,
                 log_path=None, image_min_tokens=IMAGE_MIN_TOKENS,
                 parallel=PARALLEL_SLOTS):
        self.binary = binary
        self.model_path = model_path
        self.mmproj_path = mmproj_path
        self.gpu_layers = str(gpu_layers)
        # Контекст берём свой, а не из конфига: см. SERVER_CONTEXT. Если
        # вызывающий явно просит меньше, уважаем его выбор.
        self.context_length = min(int(context_length), SERVER_CONTEXT)
        self.host = host
        self.port = int(port)
        self.threads = threads
        self.image_min_tokens = image_min_tokens
        self.parallel = parallel
        # Вывод сервера идёт в файл, а НЕ в subprocess.PIPE. Сервер пишет в
        # свой лог по строке на каждый запрос; если конец трубы никто не
        # читает, её буфер заполняется, сервер блокируется на записи и
        # перестаёт отвечать — при живом /health и простаивающей видеокарте.
        self.log_path = log_path or os.path.join(
            os.path.dirname(os.path.abspath(model_path)) or ".",
            "llama-server.log")
        self.proc = None
        self._log_file = None

    @property
    def url(self):
        return "http://%s:%d" % (self.host, self.port)

    def start(self):
        """Поднимает сервер и ждёт готовности. True, если готов."""
        cmd = [
            self.binary,
            "-m", self.model_path,
            "--mmproj", self.mmproj_path,
            "-ngl", self.gpu_layers,
            "-c", str(self.context_length),
            "--host", self.host,
            "--port", str(self.port),
            "--no-warmup",
            # Изоляция документов друг от друга: см. PARALLEL_SLOTS.
            "--parallel", str(self.parallel),
        ]
        if self.threads:
            cmd += ["--threads", str(self.threads)]
        if self.image_min_tokens:
            cmd += ["--image-min-tokens", str(self.image_min_tokens)]

        log.info("запуск llama-server", extra={
            "port": self.port, "gpu_layers": self.gpu_layers,
            "context_length": self.context_length,
            "image_min_tokens": self.image_min_tokens,
            "parallel": self.parallel,
            "server_log": self.log_path})
        try:
            self._log_file = open(self.log_path, "a", encoding="utf-8",
                                  errors="replace")
            self.proc = subprocess.Popen(
                cmd, stdout=self._log_file, stderr=subprocess.STDOUT)
        except Exception as e:
            log.error("llama-server не запустился", extra={"error": str(e)})
            self._closeLog()
            return False

        started = time.time()
        while time.time() - started < STARTUP_TIMEOUT:
            if self.proc.poll() is not None:
                log.error("llama-server завершился при старте",
                          extra={"returncode": self.proc.returncode})
                # Прибираем так же, как по таймауту ниже: иначе останутся
                # открытый файл журнала и ссылка на мёртвый процесс, а сервис
                # живёт неделями и сервер может подниматься не один раз.
                self.stop()
                return False
            if self.healthy():
                log.info("llama-server готов", extra={
                    "seconds": round(time.time() - started, 1),
                    "url": self.url})
                return True
            time.sleep(STARTUP_POLL)

        log.error("llama-server не поднялся за отведённое время",
                  extra={"timeout": STARTUP_TIMEOUT})
        self.stop()
        return False

    def healthy(self):
        try:
            with urllib.request.urlopen(self.url + "/health", timeout=3) as r:
                return json.loads(r.read()).get("status") == "ok"
        except Exception:
            return False

    def stop(self):
        """Останавливает сервер и освобождает видеопамять."""
        if self.proc is None:
            return
        try:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
            log.info("llama-server остановлен")
        except Exception as e:
            log.warning("остановка llama-server не удалась",
                        extra={"error": str(e)})
        finally:
            self.proc = None
            self._closeLog()

    def _closeLog(self):
        if self._log_file is not None:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None

    def __enter__(self):
        if not self.start():
            raise RuntimeError("llama-server не поднялся")
        return self

    def __exit__(self, *exc):
        self.stop()
        return False

    def read(self, prompt, image_path, n_predict=2000, temperature=0,
             timeout=None):
        """Читает картинку. Возвращает (текст, токены_с_вероятностями).

        Токены — список элементов с ключами "token" и "logprob"; их
        раскладывает по значениям src/logprobs.py. Пустой список означает,
        что вероятностей не пришло: тогда logprob_min останется пустым, и это
        честнее выдуманного числа.
        """
        try:
            with open(image_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
        except Exception as e:
            log.error("картинка не прочитана", extra={
                "image": image_path, "error": str(e)})
            return "", []

        body = {
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url",
                     "image_url": {"url": "data:image/png;base64," + b64}},
                ],
            }],
            "max_tokens": int(n_predict),
            "temperature": temperature,
            # Воспроизводимость: повторный прогон того же скана обязан дать
            # тот же результат, иначе спорный случай разобрать нечем. Одной
            # temperature=0 для этого мало — сервер подставляет свои
            # top_p/top_k/min_p и случайный seed. Фиксируем всю выборку
            # явно: top_k=1 это и есть "брать самый вероятный токен".
            "top_k": 1,
            "top_p": 1.0,
            "min_p": 0.0,
            "typical_p": 1.0,
            "repeat_penalty": 1.0,
            "seed": 0,
            # Вероятность выбранного токена. top_logprobs=1 — нужен именно
            # выбранный, а не альтернативы: важно, насколько модель была
            # уверена в том, что написала.
            "logprobs": True,
            "top_logprobs": 1,
        }

        req = urllib.request.Request(
            self.url + "/v1/chat/completions",
            json.dumps(body).encode("utf-8"),
            {"Content-Type": "application/json"})

        # Ждать бесконечно нельзя: зависший запрос вешает всю очередь.
        wait = timeout if timeout else REQUEST_TIMEOUT

        started = time.time()
        try:
            with urllib.request.urlopen(req, timeout=wait) as r:
                data = json.loads(r.read())
        except socket.timeout:
            log.error("сервер не ответил за отведённое время", extra={
                "timeout": wait, "image": os.path.basename(image_path)})
            return "", []
        except urllib.error.HTTPError as e:
            log.error("сервер отклонил запрос", extra={
                "code": e.code, "body": e.read().decode("utf-8", "replace")[:500],
                "image": os.path.basename(image_path)})
            return "", []
        except Exception as e:
            log.error("запрос к серверу не прошёл", extra={
                "error": str(e), "seconds": round(time.time() - started, 1),
                "image": os.path.basename(image_path)})
            return "", []

        try:
            choice = data["choices"][0]
            text = choice["message"]["content"] or ""
            toks = ((choice.get("logprobs") or {}).get("content")) or []
        except Exception as e:
            log.error("ответ сервера не разобран", extra={"error": str(e)})
            return "", []

        # Пустой ответ при успешном HTTP — это отказ, а не пустая страница.
        # Так проявляется переполнение контекста: документ возвращается
        # пустым за пару секунд и выглядит как незаполненный.
        if not text.strip():
            log.error("сервер вернул пустой ответ", extra={
                "seconds": round(time.time() - started, 2),
                "finish_reason": choice.get("finish_reason"),
                "image": os.path.basename(image_path)})
            return "", []

        log.info("проход через сервер завершён", extra={
            "seconds": round(time.time() - started, 2),
            "output_chars": len(text),
            "tokens": len(toks),
            "image": os.path.basename(image_path)})
        return text, toks
