# -*- coding: utf-8 -*-
"""Вероятность чтения по значениям: logprob_min из ответа модели.

Проверка формата (дата календарная, в телефоне хватает цифр) подтверждает
правдоподобие значения, но не правильность чтения: неверно прочитанная
цифра даёт такой же «хороший» телефон. `logprob_min` берётся из самой модели
и говорит о другом — насколько уверенно прочитан самый сомнительный токен
значения. Минимум, а не среднее, потому что одна неуверенная цифра в номере
делает негодным весь номер, а среднее по шести уверенным токенам её
растворит.

Откуда берётся. llama-mtmd-cli отдаёт только текст, поэтому на этом пути
вероятностей нет совсем. Их отдаёт llama-server: по токену на элемент с его
logprob. Токены раскладываются по значениям разбором того самого JSON,
который модель и сгенерировала: позиция каждого токена в тексте известна,
границы каждого значения в тексте тоже, остаётся пересечь их.
"""
import math

from logging_setup import getLogger

log = getLogger("logprobs")


def fieldLogprobs(tokens, text=None):
    """Карта ключа -> (logprob_min, n_tokens) по ответу llama-server.

    `tokens` — список элементов вида {"token": "<токен>", "logprob": -0.12};
    порядок совпадает с порядком токенов в сгенерированном тексте.

    Ключи повторяют структуру ответа: "city" для поля, "address.street" для
    вложенного объекта, "lines[3]" для элемента списка.

    Возвращает пустую карту, если разобрать не удалось: пустая карта означает
    "не измеряли", а выдуманное число молча испортило бы порог у того, кто
    на него опирается.
    """
    if not tokens:
        return {}

    toks = _tokens(tokens)
    if not toks:
        return {}

    if text is None:
        text = "".join(t[0] for t in toks)

    spans = _valueSpans(text)
    if not spans:
        return {}

    # Смещение каждого токена в тексте: токены идут подряд, поэтому позиция
    # накапливается длиной. Так токен сопоставляется значению без повторного
    # поиска подстрок, который путался бы на одинаковых значениях.
    out = {}
    pos = 0
    bounds = []
    for content, lp in toks:
        bounds.append((pos, pos + len(content), lp))
        pos += len(content)

    for field, (start, end) in spans.items():
        if end <= start:
            # Пустое значение: токенов у него нет, и logprob измерять не по
            # чему. Ноль токенов — это факт, а не пропуск измерения.
            out[field] = (None, 0)
            continue
        lps = [lp for (t0, t1, lp) in bounds
               # токен относится к значению, если пересекается с его границами
               if lp is not None and t0 < end and t1 > start]
        if not lps:
            continue
        out[field] = (round(min(lps), 4), len(lps))
    return out


def _tokens(probs):
    """Нормализует элементы ответа сервера к (текст, logprob)."""
    toks = []
    for item in probs:
        if not isinstance(item, dict):
            return []
        # Текущий llama-server кладёт текст токена под "token"; "content"
        # оставлен для совместимости со старыми сборками.
        content = item.get("token")
        if content is None:
            content = item.get("content")
        if content is None:
            return []
        lp = item.get("logprob")
        if lp is None:
            # При post_sampling_probs сервер отдаёт вероятность под "prob"
            # вместо логарифма. Переводим сами: сравнивать вероятность с
            # порогом для logprob значило бы сравнивать разные шкалы.
            prob = item.get("prob")
            if isinstance(prob, (int, float)) and prob > 0:
                lp = math.log(prob)
        toks.append((str(content), lp))
    return toks


def _valueSpans(text):
    """Границы значений в сгенерированном JSON: ключ -> (начало, конец)."""
    spans = {}
    try:
        _scanObject(text, spans, prefix="", start=text.index("{"))
    except Exception as e:
        log.debug("границы значений не разобраны, logprob останется пустым",
                  extra={"error": str(e)})
        return {}
    return spans


def _skipSpace(text, i):
    n = len(text)
    while i < n and text[i] in " \t\r\n":
        i += 1
    return i


def _scanValue(text, spans, key, i):
    """Одно значение JSON начиная с позиции i. Возвращает индекс за ним.

    Свой сканер, а не json.loads: стандартный разбор возвращает значения без
    позиций, а нужны именно позиции — по ним токены и раскладываются.
    """
    n = len(text)
    if i >= n:
        return i
    ch = text[i]
    if ch == "{":
        return _scanObject(text, spans, key + ".", i)
    if ch == "[":
        return _scanArray(text, spans, key, i)
    if ch == '"':
        value_start = i + 1
        _, i = _scanString(text, i)
        # конец значения — закрывающая кавычка, её в значение не берём
        spans[key] = (value_start, i - 1)
        return i
    value_start = i
    while i < n and text[i] not in ",}]":
        i += 1
    spans[key] = (value_start, i)
    return i


def _scanObject(text, spans, prefix, start):
    """Проходит объект JSON. Возвращает индекс сразу за закрывающей скобкой."""
    i = start + 1
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "}":
            return i + 1
        if ch != '"':
            i += 1
            continue

        key, i = _scanString(text, i)
        i = _skipSpace(text, i)
        if i >= n or text[i] != ":":
            continue
        i = _skipSpace(text, i + 1)
        if i >= n:
            break
        i = _scanValue(text, spans, prefix + key, i)
    return i


def _scanArray(text, spans, key, start):
    """Проходит список JSON: элементы получают ключи вида "lines[0]"."""
    i = start + 1
    n = len(text)
    index = 0
    while i < n:
        i = _skipSpace(text, i)
        if i >= n:
            break
        ch = text[i]
        if ch == "]":
            return i + 1
        if ch == ",":
            i += 1
            continue
        i = _scanValue(text, spans, "%s[%d]" % (key, index), i)
        index += 1
    return i


def _scanString(text, i):
    """Строка JSON начиная с кавычки: возвращает (содержимое, индекс за ней)."""
    assert text[i] == '"'
    i += 1
    buf = []
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            buf.append(text[i + 1])
            i += 2
            continue
        if ch == '"':
            return "".join(buf), i + 1
        buf.append(ch)
        i += 1
    return "".join(buf), i
