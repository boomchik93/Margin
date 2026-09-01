# -*- coding: utf-8 -*-
"""
Тесты раскладки вероятностей чтения по значениям.

Токены приходят сплошным потоком, без привязки к полям. Ошибка раскладки
тихая: число есть, но относится к соседнему значению. Поэтому проверяется
именно привязка — чей минимум куда попал.
"""
import unittest

import support  # noqa: F401
import logprobs


def toks(*pairs):
    return [{"token": t, "logprob": lp} for t, lp in pairs]


class FieldLogprobsTest(unittest.TestCase):
    def test_минимумБерётсяПоТокенамСвоегоЗначения(self):
        tokens = toks(('{"', -0.01), ('city', -0.01), ('": "', -0.01),
                      ('Каз', -0.2), ('ань', -1.5), ('", "', -0.01),
                      ('code', -0.01), ('": "', -0.01), ('AB', -0.05),
                      ('"}', -0.01))
        out = logprobs.fieldLogprobs(tokens)
        self.assertEqual(out["city"], (-1.5, 2))
        self.assertEqual(out["code"], (-0.05, 1))

    def test_пустоеЗначениеЭтоНольТокеновАНеПропуск(self):
        tokens = toks(('{"city": "', -0.01), ('"}', -0.01))
        self.assertEqual(logprobs.fieldLogprobs(tokens)["city"], (None, 0))

    def test_элементыСпискаПолучаютИндексы(self):
        tokens = toks(('{"lines": ["', -0.01), ('первая', -0.3),
                      ('", "', -0.01), ('вторая', -2.0), ('"]}', -0.01))
        out = logprobs.fieldLogprobs(tokens)
        self.assertEqual(out["lines[0]"], (-0.3, 1))
        self.assertEqual(out["lines[1]"], (-2.0, 1))

    def test_вложенныйОбъектДаётСоставнойКлюч(self):
        tokens = toks(('{"address": {"street": "', -0.01), ('Мира', -0.7),
                      ('"}}', -0.01))
        self.assertEqual(logprobs.fieldLogprobs(tokens)["address.street"],
                         (-0.7, 1))

    def test_вероятностьПереводитсяВЛогарифм(self):
        tokens = [{"token": '{"a": "', "prob": 1.0},
                  {"token": "x", "prob": 0.5},
                  {"token": '"}', "prob": 1.0}]
        value, n = logprobs.fieldLogprobs(tokens)["a"]
        self.assertAlmostEqual(value, -0.6931, places=3)
        self.assertEqual(n, 1)

    def test_неразборчивыйВводДаётПустуюКарту(self):
        """Пустая карта означает «не измеряли». Выдуманное число хуже."""
        self.assertEqual(logprobs.fieldLogprobs([]), {})
        self.assertEqual(logprobs.fieldLogprobs(None), {})
        self.assertEqual(logprobs.fieldLogprobs(["не словарь"]), {})
        self.assertEqual(logprobs.fieldLogprobs(toks(("просто текст", -0.1))),
                         {})


if __name__ == "__main__":
    unittest.main()
