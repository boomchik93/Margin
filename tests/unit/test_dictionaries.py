# -*- coding: utf-8 -*-
"""
Тесты сверки со словарями.

Сверка — единственное место пайплайна, которое вправе переписать прочитанное
значение. Поэтому проверяется главным образом то, чего она делать НЕ должна:
подставлять наугад, выбирать между равными кандидатами и останавливать
распознавание из-за битого файла.
"""
import json
import os
import shutil
import unittest

import support
import dictmatch
import schema as schemas

CITIES = ["Москва", "Казань", "Самара", "Саратов", "Уфа", "Пермь",
          "Нижний Новгород"]


class DistanceTest(unittest.TestCase):
    def test_одинаковыеСтроки(self):
        self.assertEqual(dictmatch.confusableDist("КАЗАНЬ", "КАЗАНЬ"), 0.0)

    def test_похожиеБуквыСтоятПоловину(self):
        # Ь и Б путаются в рукописи
        self.assertEqual(dictmatch.confusableDist("КАЗАНЬ", "КАЗАНБ"), 0.5)

    def test_непохожиеБуквыСтоятЦелую(self):
        self.assertEqual(dictmatch.confusableDist("КАЗАНЬ", "КАЗАНК"), 1.0)

    def test_пропущеннаяБукваСтоитЦелую(self):
        self.assertEqual(dictmatch.confusableDist("КАЗАНЬ", "КАЗАН"), 1.0)

    def test_ключСравненияСнимаетРегистрИЁ(self):
        self.assertEqual(dictmatch.norm("  орёл  "), "ОРЕЛ")


class MatchValueTest(unittest.TestCase):
    def test_точноеСовпадениеПриводитКНаписаниюИзСловаря(self):
        self.assertEqual(dictmatch.matchValue("КАЗАНЬ", CITIES),
                         ("Казань", "exact"))
        self.assertEqual(dictmatch.matchValue("нижний  новгород", CITIES),
                         ("Нижний Новгород", "exact"))

    def test_безПорогаСловарьТолькоПодсказывает(self):
        """Порог доверия набирается замером. Пока его нет, значение не
        переписывается — иначе сверка чинила бы вслепую."""
        self.assertEqual(dictmatch.matchValue("Казанб", CITIES),
                         ("Казанб", "review"))

    def test_сПорогомОпискаИсправляется(self):
        self.assertEqual(dictmatch.matchValue("Казанб", CITIES, max_dist=1.0),
                         ("Казань", "fixed"))

    def test_далёкоеЗначениеНеПодменяется(self):
        self.assertEqual(dictmatch.matchValue("Воркута", CITIES, max_dist=1.0),
                         ("Воркута", "review"))

    def test_равныеКандидатыНеВыбираютсяНаугад(self):
        value, status = dictmatch.matchValue(
            "Сама", ["Сами", "Саму"], max_dist=1.0)
        self.assertEqual((value, status), ("Сама", "review"))

    def test_короткоеЗначениеТолькоТочно(self):
        """У слова из трёх букв на расстоянии одной правки лежит полсловаря."""
        self.assertEqual(dictmatch.matchValue("Уфы", CITIES, max_dist=2.0),
                         ("Уфы", "review"))

    def test_пустоеЗначениеИПустойСловарьПропускаются(self):
        self.assertEqual(dictmatch.matchValue("", CITIES)[1], "skip")
        self.assertEqual(dictmatch.matchValue("Казань", [])[1], "skip")
