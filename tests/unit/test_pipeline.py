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


class ImageTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = support.tmpDir()
        self.image = support.makeImage(os.path.join(self.dir, "page.png"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class RecognizeTextTest(ImageTestCase):
    def test_текстСобираетсяИзСтрок(self):
        model = FakeModel({"text": {"lines": ["Привет,", "это письмо."]}})
        result = ocr.recognizeText(model, self.image, config())
        self.assertEqual(result["mode"], "text")
        self.assertTrue(result["parsed"])
        self.assertEqual(result["text"], "Привет,\nэто письмо.")
        self.assertEqual(result["unclear_count"], 0)
        self.assertNotIn("error", result)

    def test_неразборчивыеСловаСчитаются(self):
        model = FakeModel({"text": {"lines": ["купить [?] и [?]", "хлеб"]}})
        result = ocr.recognizeText(model, self.image, config())
        self.assertEqual(result["unclear_count"], 2)

    def test_молчаниеМоделиНеВыглядитКакПустаяСтраница(self):
        result = ocr.recognizeText(FakeModel({}), self.image, config())
        self.assertEqual(result["lines"], [])
        self.assertEqual(result["error"], "model_no_response")

    def test_пустаяСтраницаНеОшибка(self):
        result = ocr.recognizeText(FakeModel({"text": {"lines": []}}),
                                   self.image, config())
        self.assertEqual(result["lines"], [])
        self.assertNotIn("error", result)

    def test_режимЧтенияПечатногоМеняетПромпт(self):
        model = FakeModel({"text": {"lines": []}})
        ocr.recognizeText(model, self.image, config())
        ocr.recognizeText(model, self.image, config(), printed=True)
        self.assertIn("ТОЛЬКО рукописный", model.calls[0][1])
        self.assertIn("ВЕСЬ текст", model.calls[1][1])

    def test_уверенностьПоСтрокамЕстьТолькоСВероятностями(self):
        raw = '{"lines": ["да", "нет"]}'
        tokens = [{"token": '{"lines": ["', "logprob": -0.01},
                  {"token": "да", "logprob": -0.4},
                  {"token": '", "', "logprob": -0.01},
                  {"token": "нет", "logprob": -2.5},
                  {"token": '"]}', "logprob": -0.01}]
        with_tokens = ocr.recognizeText(
            FakeModel({"text": raw}, {"text": tokens}), self.image, config())
        self.assertEqual(with_tokens["line_logprob_min"], [-0.4, -2.5])

        without = ocr.recognizeText(FakeModel({"text": raw}),
                                    self.image, config())
        self.assertNotIn("line_logprob_min", without)

    def test_временныеФайлыУдаляются(self):
        seen = []

        def model(prompt, image_path, n_predict, tag):
            seen.append(image_path)
            return '{"lines": []}', []

        ocr.recognizeText(model, self.image, config())
        self.assertNotEqual(seen[0], self.image)
        self.assertFalse(os.path.exists(seen[0]))
        self.assertTrue(os.path.exists(self.image))


class RecognizeFormTest(ImageTestCase):
    def schema(self, zones=None):
        raw = dict(support.SAMPLE_SCHEMA)
        if zones:
            raw["zones"] = zones
        sch, errors = schemas.validate(raw, name="sample")
        self.assertEqual(errors, [])
        return sch

    def test_одинПроходБезЗон(self):
        model = FakeModel({"full": {"title": "Отчёт", "amount": "12"}})
        result, meta = ocr.recognizeForm(model, self.image, self.schema(),
                                         config())
        self.assertEqual(result["title"], "Отчёт")
        self.assertEqual(result["city"], "")
        self.assertTrue(meta["parsed"])
        self.assertEqual([c[0] for c in model.calls], ["full"])

    def test_неразобранныйОтветДаётПолнуюПустуюСхему(self):
        model = FakeModel({"full": "не могу прочитать"})
        result, meta = ocr.recognizeForm(model, self.image, self.schema(),
                                         config())
        self.assertEqual(result, schemas.emptyResult(self.schema()))
        self.assertFalse(meta["parsed"])
        self.assertNotIn("error", meta)

    def test_молчаниеМоделиПомечаетсяОшибкой(self):
        _, meta = ocr.recognizeForm(FakeModel({}), self.image, self.schema(),
                                    config())
        self.assertEqual(meta["error"], "model_no_response")

    def test_зонаЗаполняетПустоеНоНеЗатираетПрочитанное(self):
        sch = self.schema([{"name": "top", "box": [0, 0, 1, 0.5],
                            "fields": ["title", "city"]}])
        model = FakeModel({"full": {"title": "Отчёт", "city": ""},
                           "top": {"title": "Отчет2", "city": "Казань"}})
        result, _ = ocr.recognizeForm(model, self.image, sch, config())
        self.assertEqual(result["title"], "Отчёт")
        self.assertEqual(result["city"], "Казань")

    def test_зонаСOverrideЗамещает(self):
        sch = self.schema([{"name": "top", "box": [0, 0, 1, 0.5],
                            "fields": ["title"], "override": True}])
        model = FakeModel({"full": {"title": "Отчёт"},
                           "top": {"title": "Отчёт за год"}})
        result, _ = ocr.recognizeForm(model, self.image, sch, config())
        self.assertEqual(result["title"], "Отчёт за год")

    def test_пустойОтветЗоныСOverrideНеСтираетЗначение(self):
        sch = self.schema([{"name": "top", "box": [0, 0, 1, 0.5],
                            "fields": ["title"], "override": True}])
        model = FakeModel({"full": {"title": "Отчёт"}, "top": {"title": ""}})
        result, _ = ocr.recognizeForm(model, self.image, sch, config())
        self.assertEqual(result["title"], "Отчёт")

    def test_отметкаЗоныПринимаетсяКогдаОнаОпределённее(self):
        sch = self.schema([{"name": "marks", "box": [0, 0.5, 1, 1],
                            "fields": ["agree"]}])
        unclear = FakeModel({"full": {"agree": "unclear"},
                             "marks": {"agree": "checked"}})
        result, _ = ocr.recognizeForm(unclear, self.image, sch, config())
        self.assertEqual(result["agree"], "checked")

        sure = FakeModel({"full": {"agree": "unchecked"},
                          "marks": {"agree": "checked"}})
        result, _ = ocr.recognizeForm(sure, self.image, sch, config())
        self.assertEqual(result["agree"], "unchecked")

    def test_голосованиеСводитПроходыПосимвольно(self):
        sch = self.schema([{"name": "phone", "box": [0, 0.2, 1, 0.4],
                            "fields": ["phone"], "vote": True}])
        model = FakeModel({
            "full": {"phone": "89001234567"},
            "phone-1": {"phone": "89001284567"},
            "phone-2": {"phone": "39001234567"},
            "phone-3": {"phone": "89001234567"},
        })
        result, meta = ocr.recognizeForm(model, self.image, sch, config())
        self.assertEqual(result["phone"], "89001234567")
        self.assertEqual(meta["votes"]["phone"], {"total": 4, "agree": 2})

    def test_одинГолосЗоныНеПереписываетОсновнойПроход(self):
        sch = self.schema([{"name": "phone", "box": [0, 0.2, 1, 0.4],
                            "fields": ["phone"], "vote": True}])
        model = FakeModel({"full": {"phone": "89001234567"},
                           "phone-1": {"phone": "11111111111"}})
        result, meta = ocr.recognizeForm(model, self.image, sch, config())
        self.assertEqual(result["phone"], "89001234567")
        self.assertEqual(meta["votes"], {})

    def test_фрагментацияВыключаетсяНастройкой(self):
        sch = self.schema([{"name": "top", "box": [0, 0, 1, 0.5],
                            "fields": ["title"]}])
        model = FakeModel({"full": {"title": "Отчёт"},
                           "top": {"title": "Другое"}})
        ocr.recognizeForm(model, self.image, sch, config(fragmentation=False))
        self.assertEqual([c[0] for c in model.calls], ["full"])

    def test_вероятностьСнимаетсяСПоляСобранногоГолосованием(self):
        """Она измерена для строки основного прохода, а в результате лежит
        другая — собранная. Чужое число хуже отсутствующего."""
        sch = self.schema([{"name": "phone", "box": [0, 0.2, 1, 0.4],
                            "fields": ["phone"], "vote": True}])
        raw = '{"title": "Отчёт", "phone": "89001234567"}'
        tokens = [{"token": '{"title": "', "logprob": -0.01},
                  {"token": "Отчёт", "logprob": -0.3},
                  {"token": '", "phone": "', "logprob": -0.01},
                  {"token": "89001234567", "logprob": -1.2},
                  {"token": '"}', "logprob": -0.01}]
        model = FakeModel({"full": raw,
                           "phone-1": {"phone": "89001234567"},
                           "phone-2": {"phone": "89001234567"}},
                          {"full": tokens})
        _, meta = ocr.recognizeForm(model, self.image, sch, config())
        self.assertEqual(meta["logprobs"]["title"], (-0.3, 1))
        self.assertNotIn("phone", meta["logprobs"])


class PostprocessTest(unittest.TestCase):
    def setUp(self):
        self.dir = support.tmpDir()
        self.saved = dictmatch.DICT_DIR
        dictmatch.DICT_DIR = self.dir
        with open(os.path.join(self.dir, "cities.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"values": ["Казань", "Самара"], "max_dist": 1.0}, f,
                      ensure_ascii=False)
        dictmatch.reload()
        self.sch, errors = schemas.validate(support.SAMPLE_SCHEMA,
                                            name="sample")
        self.assertEqual(errors, [])

    def tearDown(self):
        dictmatch.DICT_DIR = self.saved
        dictmatch.reload()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_результатВсегдаСодержитВсеПоляСхемы(self):
        result = ocr.postprocessForm(self.sch, {"title": "отчёт"})
        self.assertEqual(set(result["fields"]),
                         {f["key"] for f in self.sch["fields"]})
        self.assertEqual(result["fields"]["title"], "ОТЧЁТ")

    def test_трассировкаПоказываетКаждуюСтадию(self):
        result = ocr.postprocessForm(self.sch, {"city": " казанб "})
        self.assertEqual(result["trace"]["city"], {
            "recognized": "казанб", "normalized": "казанб",
            "final": "Казань", "dict": "fixed"})
        self.assertEqual(result["dict_status"], {"city": "fixed"})

    def test_уверенностьСчитаетсяПоПрочитанномуАНеПоПодстановке(self):
        result = ocr.postprocessForm(self.sch, {"city": "казанб"})
        self.assertEqual(result["confidence_reason"]["city"], "unverified")

    def test_очередьПроверкиСобираетсяИзВсехПричин(self):
        result = ocr.postprocessForm(self.sch, {
            "title": "отчёт", "city": "Воркута", "issued": "99.99.2024",
            "agree": "checked"})
        self.assertEqual(result["needs_review"], ["issued", "city"])
        self.assertEqual(result["candidates"]["city"], [])

    def test_вероятностиПопадаютВРезультатТолькоЕслиИзмерены(self):
        plain = ocr.postprocessForm(self.sch, {"title": "отчёт"})
        self.assertNotIn("logprob_min", plain)
        measured = ocr.postprocessForm(
            self.sch, {"title": "отчёт"},
            {"logprobs": {"title": (-0.3, 1), "чужой": (-9, 1)}})
        self.assertEqual(measured["logprob_min"], {"title": -0.3})
