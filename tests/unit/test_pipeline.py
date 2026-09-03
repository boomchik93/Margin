# -*- coding: utf-8 -*-
"""
Тесты пайплайна распознавания.

Модель здесь не запускается: вместо неё подставляется функция, отдающая
заранее заданные ответы. Проверяется всё, что лежит вокруг модели, — разбор
её ответа, слияние зон, голосование и постобработка. Именно здесь ошибки
тихие: модель ответила верно, а в результат попало другое.
"""
import json
import os
import shutil
import unittest

import support
import dictmatch
import ocr
import schema as schemas


class FakeModel:
    """Подставная модель: ответы выдаются по метке прохода."""

    def __init__(self, answers, tokens=None):
        self.answers = answers
        self.tokens = tokens or {}
        self.calls = []

    def __call__(self, prompt, image_path, n_predict, tag):
        self.calls.append((tag, prompt))
        answer = self.answers.get(tag, "")
        if isinstance(answer, dict):
            answer = json.dumps(answer, ensure_ascii=False)
        return answer, self.tokens.get(tag, [])


def config(**ocr_options):
    cfg = ocr.defaultConfig()
    cfg["ocr"].update(ocr_options)
    return cfg


class ExtractJsonTest(unittest.TestCase):
    def test_чистыйJson(self):
        self.assertEqual(ocr.extractJson('{"a": "1"}'), {"a": "1"})

    def test_markdownОбёрткаСнимается(self):
        self.assertEqual(ocr.extractJson('```json\n{"a": "1"}\n```'),
                         {"a": "1"})

    def test_служебныеТокеныЧатаСрезаются(self):
        raw = ('<|im_start|>user\nвопрос<|im_end|>\n<|im_start|>assistant\n'
               '{"a": "1"}<|im_end|>')
        self.assertEqual(ocr.extractJson(raw), {"a": "1"})

    def test_текстВокругОбъектаОтбрасывается(self):
        self.assertEqual(ocr.extractJson('Вот ответ: {"a": "1"} Готово.'),
                         {"a": "1"})

    def test_скобкаВнутриСтрокиНеЛомаетРазбор(self):
        self.assertEqual(ocr.extractJson('{"a": "x } y", "b": "2"}'),
                         {"a": "x } y", "b": "2"})

    def test_хвостоваяЗапятаяЧинится(self):
        self.assertEqual(ocr.extractJson('{"a": "1", "b": ["x",],}'),
                         {"a": "1", "b": ["x"]})

    def test_неJsonДаётNone(self):
        for raw in ("", None, "просто текст", "{ оборванный"):
            self.assertIsNone(ocr.extractJson(raw))


class CharVoteTest(unittest.TestCase):
    def test_большинствоПоКаждомуСимволу(self):
        """Итог может не совпасть ни с одним голосом целиком: проходы
        ошиблись в разных символах."""
        self.assertEqual(
            ocr.charVote(["89001234567", "89001284567", "39001234567"]),
            "89001234567")

    def test_голосДругойДлиныНеУчаствуетВПосимвольномСведении(self):
        self.assertEqual(ocr.charVote(["12345", "12345", "1234"]), "12345")

    def test_приНичьейПобеждаетПервыйГолос(self):
        self.assertEqual(ocr.charVote(["АБВ", "АГВ"]), "АБВ")

    def test_пустыеГолосаНеСчитаются(self):
        self.assertEqual(ocr.charVote(["", None, "КОД"]), "КОД")
        self.assertEqual(ocr.charVote([]), "")


class ParseLinesTest(unittest.TestCase):
    def test_списокСтрок(self):
        lines, parsed = ocr.parseLines('{"lines": ["первая", " вторая  строка "]}')
        self.assertTrue(parsed)
        self.assertEqual(lines, ["первая", "вторая строка"])

    def test_пустойСписокЭтоПустаяСтраница(self):
        self.assertEqual(ocr.parseLines('{"lines": []}'), ([], True))

    def test_многоточиеИзОписанияФорматаОтбрасывается(self):
        self.assertEqual(ocr.parseLines('{"lines": ["..."]}'), ([], True))

    def test_ответБезJsonОтдаётсяСтроками(self):
        """Хуже разобранного ответа, но лучше пустого: текст клиент получит."""
        lines, parsed = ocr.parseLines("первая строка\n\nвторая строка")
        self.assertFalse(parsed)
        self.assertEqual(lines, ["первая строка", "вторая строка"])
