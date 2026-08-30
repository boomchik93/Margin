# -*- coding: utf-8 -*-
"""
Тесты нормализации.

Общее правило, которое здесь проверяется: формат приводится, данные не
выдумываются. Невозможное значение остаётся как прочитано и уходит на ручную
проверку, а не «чинится» до правдоподобного.
"""
import datetime
import unittest

import support  # noqa: F401
import normalize
import schema as schemas


def field(ftype, **extra):
    return {"key": "f", "label": "f", "type": ftype, **extra}


class DateTest(unittest.TestCase):
    TODAY = datetime.date(2026, 6, 1)

    def test_разделителиПриводятсяКТочкам(self):
        for raw in ("1.2.2024", "01/02/2024", "1-2-2024", "01 02 2024"):
            self.assertEqual(normalize.normalizeDate(raw, self.TODAY),
                             "01.02.2024", raw)

    def test_двузначныйГодНеУходитВБудущее(self):
        self.assertEqual(normalize.normalizeDate("05.03.24", self.TODAY),
                         "05.03.2024")
        self.assertEqual(normalize.normalizeDate("05.03.85", self.TODAY),
                         "05.03.1985")

    def test_неполнаяДатаОстаётсяКакЕсть(self):
        for raw in ("март 2024", "05.03", "5.3.202"):
            self.assertEqual(normalize.normalizeDate(raw, self.TODAY), raw)

    def test_невозможнаяДатаНеЧинится(self):
        """45-е число — ошибка чтения. Исправить её нечем, и честнее отдать
        как прочитано: формат нарушен, поле уйдёт на проверку."""
        self.assertEqual(normalize.normalizeDate("45.13.2024", self.TODAY),
                         "45.13.2024")

    def test_пустоеОстаётсяПустым(self):
        self.assertEqual(normalize.normalizeDate("", self.TODAY), "")
        self.assertEqual(normalize.normalizeDate(None, self.TODAY), "")


class PhoneTest(unittest.TestCase):
    def test_остаютсяТолькоЦифры(self):
        f = field("phone")
        self.assertEqual(normalize.normalizeField(f, "8 (900) 123-45-67"),
                         "89001234567")

    def test_плюсВНачалеСохраняется(self):
        f = field("phone")
        self.assertEqual(normalize.normalizeField(f, "+7 900 123 45 67"),
                         "+79001234567")

    def test_цифрыНеДописываются(self):
        self.assertEqual(normalize.normalizeField(field("phone"), "123-45"),
                         "12345")


class TextTest(unittest.TestCase):
    def test_пробелыСхлопываются(self):
        self.assertEqual(
            normalize.normalizeField(field("text"), "  два   слова \n"),
            "два слова")

    def test_регистрПоНастройкеПоля(self):
        self.assertEqual(
            normalize.normalizeField(field("text", case="upper"), "Отчёт"),
            "ОТЧЁТ")
        self.assertEqual(
            normalize.normalizeField(field("text", case="lower"), "Отчёт"),
            "отчёт")
        self.assertEqual(
            normalize.normalizeField(field("text"), "Отчёт"), "Отчёт")

    def test_латинскаяБукваВРусскомСловеЗаменяется(self):
        # первая буква — латинская P
        self.assertEqual(normalize.fixHomoglyphs("PОМАШКА"), "РОМАШКА")

    def test_латинскоеСловоЦеликомНеТрогается(self):
        """Корпус «B2» или код «AC-15» — отдельные латинские слова, и
        переводить их в кириллицу значило бы испортить значение."""
        self.assertEqual(normalize.fixHomoglyphs("корпус B2"), "корпус B2")
        self.assertEqual(normalize.fixHomoglyphs("ACME"), "ACME")

    def test_дляНерусскогоЯзыкаГомоглифыНеСнимаются(self):
        self.assertEqual(
            normalize.normalizeField(field("text"), "PОМАШКА", language="en"),
            "PОМАШКА")


class ResultTest(unittest.TestCase):
    def test_нормализуютсяВсеПоляСхемы(self):
        sch, errors = schemas.validate(support.SAMPLE_SCHEMA)
        self.assertEqual(errors, [])
        out = normalize.normalizeResult(sch, {
            "title": " годовой  отчёт", "amount": "1 250", "issued": "1/2/24",
            "phone": "8 900 123-45-67", "agree": True})
        self.assertEqual(out["title"], "ГОДОВОЙ ОТЧЁТ")
        self.assertEqual(out["amount"], "1250")
        self.assertEqual(out["issued"], "01.02.2024")
        self.assertEqual(out["phone"], "89001234567")
        self.assertEqual(out["agree"], "checked")
        self.assertEqual(out["city"], "")


if __name__ == "__main__":
    unittest.main()
