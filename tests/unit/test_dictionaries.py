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


class DirectoryTest(unittest.TestCase):
    """Загрузка из директории."""

    def setUp(self):
        self.dir = support.tmpDir()
        self.saved = dictmatch.DICT_DIR
        dictmatch.DICT_DIR = self.dir

    def tearDown(self):
        dictmatch.DICT_DIR = self.saved
        dictmatch.reload()
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, name, payload):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
            f.write(payload if isinstance(payload, str)
                    else json.dumps(payload, ensure_ascii=False))

    def schema(self, **field):
        sch, errors = schemas.validate({"fields": [
            {"key": "city", "type": "text", "dictionary": "cities", **field}]})
        self.assertEqual(errors, [])
        return sch

    def test_списокИОбъектЧитаютсяОдинаково(self):
        self.write("cities.json", CITIES)
        self.write("units.json", {"values": ["кг", "шт"]})
        state = dictmatch.reload()
        self.assertEqual(state["dictionaries"]["cities"]["values"], len(CITIES))
        self.assertEqual(dictmatch.DB["units"], ["кг", "шт"])
        self.assertEqual(state["errors"], [])

    def test_битыйФайлНеОстанавливаетОстальные(self):
        """Словари правят руками, и лишняя запятая не должна лишать сверки
        все остальные поля."""
        self.write("cities.json", CITIES)
        self.write("broken.json", "[\"a\",")
        state = dictmatch.reload()
        self.assertIn("cities", dictmatch.DB)
        self.assertNotIn("broken", dictmatch.DB)
        self.assertEqual(len(state["errors"]), 1)
        self.assertIn("broken.json", state["errors"][0])

    def test_дублиИПустыеЗначенияСчитаются(self):
        self.write("cities.json", ["Казань", "казань", "", None, "Уфа"])
        state = dictmatch.reload()
        self.assertEqual(dictmatch.DB["cities"], ["Казань", "Уфа"])
        self.assertEqual(state["dictionaries"]["cities"]["skipped"], 3)

    def test_версияМеняетсяВместеССодержимым(self):
        self.write("cities.json", CITIES)
        dictmatch.reload()
        first = dictmatch.dictVersion()
        self.write("cities.json", CITIES + ["Омск"])
        dictmatch.reload()
        self.assertNotEqual(first, dictmatch.dictVersion())

    def test_изменениеФайлаПодхватываетсяПередСледующейСтраницей(self):
        self.write("cities.json", CITIES)
        dictmatch.reload()
        self.assertFalse(dictmatch.ensureFresh())
        self.write("cities.json", CITIES + ["Омск", "Томск"])
        self.assertTrue(dictmatch.ensureFresh())
        self.assertIn("Томск", dictmatch.DB["cities"])

    def test_порогИзФайлаВключаетИсправление(self):
        self.write("cities.json", {"values": CITIES, "max_dist": 1.0})
        dictmatch.reload()
        data, status, candidates = dictmatch.applyDictionaries(
            self.schema(), {"city": "Казанб"})
        self.assertEqual(data["city"], "Казань")
        self.assertEqual(status, {"city": "fixed"})
        self.assertEqual(candidates, {})

    def test_порогПоляВажнееПорогаФайла(self):
        self.write("cities.json", {"values": CITIES, "max_dist": 0.4})
        dictmatch.reload()
        data, status, _ = dictmatch.applyDictionaries(
            self.schema(max_dist=1.0), {"city": "Казанб"})
        self.assertEqual((data["city"], status["city"]), ("Казань", "fixed"))

    def test_ненайденноеУходитНаПроверкуСКандидатами(self):
        self.write("cities.json", CITIES)
        dictmatch.reload()
        data, status, candidates = dictmatch.applyDictionaries(
            self.schema(), {"city": "Самар"})
        self.assertEqual(data["city"], "Самар")
        self.assertEqual(status["city"], "review")
        self.assertEqual(candidates["city"][0]["value"], "Самара")

    def test_отсутствующийСловарьНеВыглядитКакПромах(self):
        """Словарь назван в схеме, а файла нет — это сбой настройки, и
        показывать его как «значение не найдено» нельзя."""
        dictmatch.reload()
        data, status, _ = dictmatch.applyDictionaries(
            self.schema(), {"city": "Казань"})
        self.assertEqual(data["city"], "Казань")
        self.assertEqual(status["city"], "no_dictionary")


class ShippedDictionaryTest(unittest.TestCase):
    def test_примерСловаряРазбирается(self):
        path = os.path.join(support.ROOT, "data", "dictionaries", "cities.json")
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        self.assertTrue(payload["values"])


if __name__ == "__main__":
    unittest.main()
