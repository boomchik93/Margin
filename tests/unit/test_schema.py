# -*- coding: utf-8 -*-
"""
Тесты схем полей.

Схему пишет человек, и ошибётся он обязательно. Проверяется, что ошибка
называется словами и не превращается в молча потерянное поле, а также то,
что из схемы собирается: пустой результат, приведение ответа модели и
промпты.
"""
import copy
import json
import os
import shutil
import unittest

import support
import schema as schemas


def valid():
    return copy.deepcopy(support.SAMPLE_SCHEMA)


class ValidateTest(unittest.TestCase):
    def test_корректнаяСхемаПроходит(self):
        sch, errors = schemas.validate(valid(), name="sample")
        self.assertEqual(errors, [])
        self.assertEqual(sch["name"], "sample")
        self.assertEqual([f["key"] for f in sch["fields"]],
                         ["title", "city", "code", "amount", "issued",
                          "phone", "agree"])

    def test_типПоУмолчаниюТекст(self):
        sch, errors = schemas.validate({"fields": [{"key": "note"}]})
        self.assertEqual(errors, [])
        self.assertEqual(sch["fields"][0]["type"], "text")
        self.assertEqual(sch["fields"][0]["label"], "note")

    def test_безПолейОтклоняется(self):
        for raw in ({}, {"fields": []}, {"fields": "x"}, [], None):
            sch, errors = schemas.validate(raw)
            self.assertIsNone(sch)
            self.assertTrue(errors)

    def test_неверныйКлючНазываетсяВОшибке(self):
        sch, errors = schemas.validate({"fields": [{"key": "Имя поля"}]})
        self.assertIsNone(sch)
        self.assertIn("key", errors[0])

    def test_повторКлючаОтклоняется(self):
        sch, errors = schemas.validate(
            {"fields": [{"key": "a"}, {"key": "a"}]})
        self.assertIsNone(sch)
        self.assertTrue(any("повторяется" in e for e in errors))

    def test_неизвестныйТипОтклоняется(self):
        sch, errors = schemas.validate(
            {"fields": [{"key": "a", "type": "money"}]})
        self.assertIsNone(sch)
        self.assertTrue(any("type" in e for e in errors))

    def test_битоеРегулярноеВыражениеОтклоняется(self):
        sch, errors = schemas.validate(
            {"fields": [{"key": "a", "format": "([a-z"}]})
        self.assertIsNone(sch)
        self.assertTrue(any("format" in e for e in errors))

    def test_однаОшибкаНеДаётЧастичнойСхемы(self):
        """Работать по наполовину разобранной схеме значило бы молча терять
        поля: клиент ждёт семь ключей, а получает шесть."""
        raw = valid()
        raw["fields"].append({"key": "bad", "type": "nope"})
        sch, errors = schemas.validate(raw)
        self.assertIsNone(sch)
        self.assertEqual(len(errors), 1)


class ZoneValidateTest(unittest.TestCase):
    def raw(self, **zone):
        raw = valid()
        base = {"name": "top", "box": [0.0, 0.0, 1.0, 0.3],
                "fields": ["title"]}
        base.update(zone)
        raw["zones"] = [base]
        return raw

    def test_корректнаяЗона(self):
        sch, errors = schemas.validate(self.raw(vote=True))
        self.assertEqual(errors, [])
        self.assertTrue(sch["zones"][0]["vote"])

    def test_рамкаВнеСтраницыОтклоняется(self):
        for box in ([0, 0, 1.2, 1], [0.5, 0, 0.2, 1], [0, 0, 1], "x"):
            sch, errors = schemas.validate(self.raw(box=box))
            self.assertIsNone(sch, box)
            self.assertTrue(any("box" in e for e in errors))

    def test_полеВнеСхемыОтклоняется(self):
        sch, errors = schemas.validate(self.raw(fields=["nope"]))
        self.assertIsNone(sch)
        self.assertTrue(any("nope" in e for e in errors))

    def test_голосованиеТолькоДляОдногоПоля(self):
        sch, errors = schemas.validate(
            self.raw(fields=["title", "city"], vote=True))
        self.assertIsNone(sch)
        self.assertTrue(any("vote" in e for e in errors))

    def test_голосованиеНеДляОтметок(self):
        sch, errors = schemas.validate(self.raw(fields=["agree"], vote=True))
        self.assertIsNone(sch)
