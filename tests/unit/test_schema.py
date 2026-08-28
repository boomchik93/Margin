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


class ResultTest(unittest.TestCase):
    def setUp(self):
        self.sch, _ = schemas.validate(valid())

    def test_пустойРезультатСодержитВсеКлючи(self):
        empty = schemas.emptyResult(self.sch)
        self.assertEqual(set(empty), {f["key"] for f in self.sch["fields"]})
        self.assertEqual(empty["title"], "")
        # Пока документ не прочитан, «отметки нет» утверждать не на чем.
        self.assertEqual(empty["agree"], schemas.UNCLEAR)

    def test_приведениеДобавляетОтсутствующиеИВыбрасываетЛишние(self):
        out = schemas.enforce(self.sch, {"title": " Отчёт ", "extra": "x"})
        self.assertEqual(out["title"], "Отчёт")
        self.assertNotIn("extra", out)
        self.assertEqual(out["city"], "")

    def test_мусорВместоОтветаДаётПустуюСхему(self):
        for junk in (None, "текст", [1, 2], 5):
            self.assertEqual(schemas.enforce(self.sch, junk),
                             schemas.emptyResult(self.sch))

    def test_вложеннаяСтруктураВместоЗначенияНеПопадаетВПоле(self):
        out = schemas.enforce(self.sch, {"title": {"a": 1}, "amount": 12})
        self.assertEqual(out["title"], "")
        self.assertEqual(out["amount"], "12")

    def test_отметкаПриводитсяКТройкеЗначений(self):
        cases = {True: "checked", False: "unchecked", "да": "checked",
                 "нет": "unchecked", "V": "checked", None: "unclear",
                 "кажется": "unclear", "UNCHECKED": "unchecked"}
        for raw, expected in cases.items():
            self.assertEqual(schemas.toCheckbox(raw), expected, raw)


class PromptTest(unittest.TestCase):
    def setUp(self):
        raw = valid()
        raw["zones"] = [
            {"name": "top", "box": [0, 0, 1, 0.3], "fields": ["title", "city"]},
            {"name": "phone", "box": [0, 0.3, 1, 0.4], "fields": ["phone"],
             "vote": True},
        ]
        self.sch, errors = schemas.validate(raw)
        self.assertEqual(errors, [])

    def test_полныйПромптНазываетВсеПоля(self):
        prompt = schemas.fullPrompt(self.sch)
        for field in self.sch["fields"]:
            self.assertIn('"%s"' % field["key"], prompt)
            self.assertIn(field["label"], prompt)

    def test_образецОтветаРазбираетсяКакJsonСПустымиЗначениями(self):
        """Образец пустой намеренно: заполненный пример модель переписывает
        в пустое поле как прочитанное значение."""
        prompt = schemas.fullPrompt(self.sch)
        sample = json.loads(prompt.strip().splitlines()[-1])
        self.assertEqual(set(sample), {f["key"] for f in self.sch["fields"]})
        self.assertEqual(sample["title"], "")
        self.assertEqual(sample["phone"], "")

    def test_правилоПроЯзыкЗависитОтСхемы(self):
        self.assertIn("кириллицей", schemas.fullPrompt(self.sch))
        raw = valid()
        raw["language"] = "en"
        sch, _ = schemas.validate(raw)
        self.assertIn("латиницей", schemas.fullPrompt(sch))

    def test_зонныйПромптТолькоПроСвоиПоля(self):
        prompts = schemas.zonePrompts(self.sch, self.sch["zones"][0])
        self.assertEqual(len(prompts), 1)
        self.assertIn('"title"', prompts[0])
        self.assertNotIn('"phone"', prompts[0])

    def test_голосованиеДаётРазныеФормулировки(self):
        """При температуре 0 повтор одного промпта даёт тот же ответ:
        независимые прочтения получаются только сменой формулировки."""
        prompts = schemas.zonePrompts(self.sch, self.sch["zones"][1])
        self.assertEqual(len(prompts), schemas.VOTE_PASSES)
        self.assertEqual(len(set(prompts)), len(prompts))
        for p in prompts:
            self.assertIn('{"phone": ""}', p)


class LoadTest(unittest.TestCase):
    """Схемы из директории: битый файл виден и не мешает остальным."""

    def setUp(self):
        self.dir = support.tmpDir()
        self.saved = schemas.SCHEMA_DIR
        schemas.SCHEMA_DIR = self.dir

    def tearDown(self):
        schemas.SCHEMA_DIR = self.saved
        schemas.reload()
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, name, payload):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
            f.write(payload if isinstance(payload, str)
                    else json.dumps(payload, ensure_ascii=False))

    def test_имяФайлаСтановитсяИменемСхемы(self):
        self.write("letter.json", valid())
        schemas.reload()
        self.assertIsNotNone(schemas.get("letter"))
        self.assertIsNone(schemas.get("other"))

    def test_битыйФайлНеМешаетОстальным(self):
        self.write("good.json", valid())
        self.write("broken.json", "{ не json")
        self.write("wrong.json", {"fields": [{"key": "a", "type": "nope"}]})
        schemas.reload()
        self.assertEqual(sorted(schemas.listSchemas()), ["good"])
        self.assertEqual(sorted(schemas.loadErrors()),
                         ["broken.json", "wrong.json"])

    def test_новыйФайлПодхватываетсяБезПерезапуска(self):
        schemas.reload()
        self.assertIsNone(schemas.get("later"))
        self.write("later.json", valid())
        self.assertIsNotNone(schemas.get("later"))


class ShippedSchemaTest(unittest.TestCase):
    """Схема-пример из config/schemas обязана проходить проверку."""

    def test_примерЗагружается(self):
        path = os.path.join(support.ROOT, "config", "schemas",
                            "example_form.json")
        with open(path, encoding="utf-8") as f:
            sch, errors = schemas.validate(json.load(f), name="example_form")
        self.assertEqual(errors, [])
        self.assertTrue(sch["fields"])


if __name__ == "__main__":
    unittest.main()
