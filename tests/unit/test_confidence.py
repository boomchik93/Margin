# -*- coding: utf-8 -*-
"""
Тесты оценки уверенности и очереди ручной проверки.

Оценка отвечает на один вопрос: похоже ли значение на то, чем оно должно
быть. От неё зависит, увидит ли человек сомнительное поле, поэтому
проверяются обе стороны — брак ловится, а нормальное значение в очередь не
попадает.
"""
import unittest

import support
import confidence
import schema as schemas


class FieldTest(unittest.TestCase):
    def setUp(self):
        sch, errors = schemas.validate(support.SAMPLE_SCHEMA)
        self.assertEqual(errors, [])
        self.fields = {f["key"]: f for f in sch["fields"]}

    def score(self, key, value):
        return confidence.fieldConfidence(self.fields[key], value)

    def test_пустоеПолеНеОценивается(self):
        self.assertEqual(self.score("title", ""), (0, "empty"))

    def test_свободныйТекстПроверитьНечем(self):
        self.assertEqual(self.score("title", "ГОДОВОЙ ОТЧЁТ"),
                         (confidence.FORMAT_NONE, "unverified"))

    def test_зацикленныйТекстСчитаетсяБраком(self):
        self.assertEqual(self.score("title", "ОТЧЁЁЁЁЁТ")[1], "format_bad")

    def test_смешаннаяЛатиницаИКириллицаВСловеСчитаетсяБраком(self):
        self.assertEqual(self.score("title", "ОТЧЁWТ")[1], "format_bad")

    def test_форматПоРегулярномуВыражению(self):
        self.assertEqual(self.score("code", "AB-1234")[1], "format_ok")
        self.assertEqual(self.score("code", "AB-123")[1], "format_bad")

    def test_дата(self):
        self.assertEqual(self.score("issued", "01.02.2024")[1], "format_ok")
        self.assertEqual(self.score("issued", "45.02.2024")[1], "format_bad")
        self.assertEqual(self.score("issued", "01.13.2024")[1], "format_bad")
        self.assertEqual(self.score("issued", "1 февраля")[1], "format_bad")
        # раньше min_year схемы
        self.assertEqual(self.score("issued", "01.02.1985")[1], "format_bad")

    def test_числоИГраницы(self):
        self.assertEqual(self.score("amount", "12")[1], "format_ok")
        self.assertEqual(self.score("amount", "12,5")[1], "format_ok")
        self.assertEqual(self.score("amount", "250")[1], "format_bad")
        self.assertEqual(self.score("amount", "двенадцать")[1], "format_bad")

    def test_телефон(self):
        self.assertEqual(self.score("phone", "89001234567")[1], "format_ok")
        self.assertEqual(self.score("phone", "12345")[1], "format_bad")

    def test_телефонСЗаданнымЧисломЦифр(self):
        field = dict(self.fields["phone"], digits=11)
        self.assertEqual(
            confidence.fieldConfidence(field, "8900123456")[1], "format_bad")

    def test_отметка(self):
        self.assertEqual(self.score("agree", "checked")[1], "mark")
        self.assertEqual(self.score("agree", "unchecked")[1], "mark")
        self.assertEqual(self.score("agree", "unclear"),
                         (confidence.FORMAT_BAD, "unclear"))


class ReviewQueueTest(unittest.TestCase):
    def setUp(self):
        raw = dict(support.SAMPLE_SCHEMA)
        raw["fields"] = [dict(f) for f in raw["fields"]]
        raw["fields"][0]["required"] = True
        self.sch, errors = schemas.validate(raw)
        self.assertEqual(errors, [])

    def queue(self, data, dict_status=None):
        full = schemas.enforce(self.sch, data)
        scores, _ = confidence.scoreResult(self.sch, full)
        return confidence.reviewQueue(self.sch, full, scores, dict_status)

    def test_хорошиеЗначенияВОчередьНеПопадают(self):
        self.assertEqual(self.queue({
            "title": "ОТЧЁТ", "issued": "01.02.2024", "agree": "checked"}), [])

    def test_бракФорматаПопадает(self):
        self.assertEqual(self.queue({
            "title": "ОТЧЁТ", "issued": "99.99.2024", "agree": "checked"}),
            ["issued"])

    def test_неразличимаяОтметкаПопадает(self):
        self.assertIn("agree", self.queue({"title": "ОТЧЁТ"}))

    def test_отсутствиеВСловареПопадает(self):
        queue = self.queue({"title": "ОТЧЁТ", "city": "Самар",
                            "agree": "checked"}, {"city": "review"})
        self.assertEqual(queue, ["city"])

    def test_пустоеОбязательноеПолеПопадает(self):
        self.assertEqual(self.queue({"agree": "checked"}), ["title"])

    def test_худшиеИдутПервыми(self):
        queue = self.queue({"issued": "99.99.2024", "city": "Самар",
                            "agree": "checked"}, {"city": "review"})
        self.assertEqual(queue, ["title", "issued", "city"])


if __name__ == "__main__":
    unittest.main()
