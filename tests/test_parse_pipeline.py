import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.services.context_interpreter import ContextInterpreter
from app.services.parse_pipeline import stage_a_extract, stage_c_prepare_for_matching
from app.services.parse_types import ContextualizedLine, RawExtractedLine
from app.services.vision_extract_service import VisionExtractService


class ParsePipelineTests(unittest.TestCase):
    def setUp(self):
        self.interpreter = ContextInterpreter(api_key="", model="gpt-4o")

    def test_grouped_deck_board_list_under_color_header(self):
        stage_a = [
            RawExtractedLine(line_id="H1", raw_text="Cinnamon Cove", section_type="header", section_header="Cinnamon Cove"),
            RawExtractedLine(line_id="L1", raw_text="56 Grooved x 12'", quantity=56),
            RawExtractedLine(line_id="L2", raw_text="13 Round x 16'", quantity=13),
        ]
        contextualized = self.interpreter.interpret(stage_a)
        self.assertEqual(contextualized[0].inherited_section_header, "Cinnamon Cove")
        self.assertIn("Cinnamon Cove", contextualized[0].normalized_description)
        self.assertIn("Cinnamon Cove", contextualized[1].normalized_description)

    def test_railing_section_header_context_applies_until_next_header(self):
        stage_a = [
            RawExtractedLine(line_id="H1", raw_text="Black Tuscany", section_type="header", section_header="Black Tuscany"),
            RawExtractedLine(line_id="L1", raw_text="11 37\" Posts", quantity=11),
            RawExtractedLine(line_id="L2", raw_text="2 Level sections x 6'", quantity=2),
            RawExtractedLine(line_id="H2", raw_text="Pressure Treated", section_type="header", section_header="Pressure Treated"),
            RawExtractedLine(line_id="L3", raw_text="4 2x12x16", quantity=4),
        ]
        contextualized = self.interpreter.interpret(stage_a)
        self.assertEqual(contextualized[0].inherited_section_header, "Black Tuscany")
        self.assertEqual(contextualized[1].inherited_section_header, "Black Tuscany")
        self.assertEqual(contextualized[2].inherited_section_header, "Pressure Treated")

    def test_accessories_after_main_product_groups_keep_context(self):
        stage_a = [
            RawExtractedLine(line_id="H1", raw_text="Cinnamon Cove", section_type="header", section_header="Cinnamon Cove"),
            RawExtractedLine(line_id="L1", raw_text="6 Risers x 12' with screws", quantity=6),
            RawExtractedLine(line_id="L2", raw_text="1 box hidden fasteners", quantity=1),
        ]
        contextualized = self.interpreter.interpret(stage_a)
        self.assertTrue(all(line.inherited_section_header == "Cinnamon Cove" for line in contextualized))

    def test_pressure_treated_lines_context(self):
        stage_a = [
            RawExtractedLine(line_id="H1", raw_text="Pressure Treated", section_type="header", section_header="Pressure Treated"),
            RawExtractedLine(line_id="L1", raw_text="4 2x12x16", quantity=4),
            RawExtractedLine(line_id="L2", raw_text="3 2x12x20", quantity=3),
        ]
        contextualized = self.interpreter.interpret(stage_a)
        for line in contextualized:
            self.assertIn("Pressure Treated", line.normalized_description)

    def test_ambiguous_shorthand_flagged(self):
        stage_a = [RawExtractedLine(line_id="L1", raw_text="misc trim pcs", quantity=2)]
        contextualized = self.interpreter.interpret(stage_a)
        self.assertIn("ambiguous_shorthand", contextualized[0].ambiguity_flags)

    def test_match_ready_needs_review_when_ambiguous(self):
        lines = [
            ContextualizedLine(
                line_id="L1",
                raw_text="misc trim pcs",
                normalized_description="trim pieces",
                quantity=2,
                ambiguity_flags=["ambiguous_shorthand"],
                inherited_section_header="Trim",
            )
        ]
        ready = stage_c_prepare_for_matching(lines)
        self.assertTrue(ready[0].needs_review)
        self.assertIn("trim pieces", ready[0].match_text)

    def test_stage_a_prefixes_line_ids_per_file(self):
        class StubVisionService:
            def extract_document(self, _file_paths, upload_context=""):
                self.upload_context = upload_context
                return {
                    "document_context": {},
                    "lines": [
                        {"line_id": "L1", "raw_text": "first row"},
                        {"line_id": "L2", "raw_text": "second row"},
                        {"file_index": 2, "line_id": "L1", "raw_text": "third row"},
                        {"file_index": 2, "line_id": "L2", "raw_text": "fourth row"},
                    ],
                }

        lines, context = stage_a_extract(
            [Path("one.pdf"), Path("two.pdf")],
            StubVisionService(),
            upload_context="Customer competitor comparison list",
        )

        self.assertEqual(
            [line.line_id for line in lines],
            ["F1-L1", "F1-L2", "F2-L1", "F2-L2"],
        )
        self.assertEqual(context, {})

    def test_stage_a_keeps_original_file_order_for_mixed_uploads(self):
        class StubVisionService:
            def extract_document(self, _file_paths, upload_context=""):
                return {
                    "document_context": {},
                    "lines": [{"file_index": 1, "line_id": "L1", "raw_text": "image row"}],
                }

            def extract(self, _file_path, upload_context=""):
                return [{"line_id": "L1", "raw_text": "csv row"}]

        lines, _ = stage_a_extract(
            [Path("notes.csv"), Path("photo.jpg")],
            StubVisionService(),
        )

        self.assertEqual([line.line_id for line in lines], ["F1-L1", "F2-L1"])
        self.assertEqual([line.raw_text for line in lines], ["csv row", "image row"])

    def test_stage_a_returns_document_context_from_vision_service(self):
        class StubVisionService:
            def extract_document(self, _file_paths, upload_context=""):
                return {
                    "document_context": {"customer_name": "Smith", "global_material_context": ["Trex"]},
                    "lines": [{"file_index": 1, "line_id": "L1", "raw_text": "12 boards"}],
                }

        lines, context = stage_a_extract([Path("one.jpg")], StubVisionService())

        self.assertEqual([line.line_id for line in lines], ["F1-L1"])
        self.assertEqual(context["customer_name"], "Smith")

    def test_vision_extract_service_uses_webp_mime(self):
        with TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "sample.webp"
            file_path.write_bytes(b"RIFF0000WEBPVP8 ")

            service = VisionExtractService(api_key="test", model="gpt-4o")
            payload = service._build_content(file_path)

        self.assertIn("data:image/webp;base64,", payload["image_url"])

    def test_stage_a_prompt_includes_upload_context_and_document_scope(self):
        service = VisionExtractService(api_key="test", model="gpt-4o")
        prompt = service._build_stage_a_prompt(
            file_count=2,
            upload_context="Customer list, some rows inherit color from notes.",
        )

        self.assertIn("customer or competitor material lists", prompt)
        self.assertIn("Treat all provided pages/images as one document set", prompt)
        self.assertIn("User-provided upload context", prompt)

    def test_vision_extract_service_normalizes_bad_quantity_strings_without_crashing(self):
        service = VisionExtractService(api_key="test", model="gpt-4o")
        normalized = service._normalize_line(
            1,
            {
                "line_id": "L1",
                "raw_text": "deck board",
                "quantity": "1-1/16",
                "quantity_raw": "1-1/16",
            },
        )

        self.assertEqual(normalized["quantity"], 1.0)

    def test_context_interpreter_passes_upload_context_to_model_prompt(self):
        captured = {}

        class FakeResponses:
            def create(self, **kwargs):
                captured["input"] = kwargs["input"]
                class Response:
                    output_text = '{"contextualized_lines":[]}'
                return Response()

        class FakeClient:
            def __init__(self, api_key):
                self.responses = FakeResponses()

        interpreter = ContextInterpreter(api_key="test", model="gpt-4o")
        with patch("app.services.context_interpreter.OpenAI", FakeClient):
            interpreter.interpret(
                [RawExtractedLine(line_id="L1", raw_text="12 boards")],
                upload_context="Competitor takeoff with shorthand notes",
            )

        prompt_text = captured["input"][0]["content"][0]["text"]
        self.assertIn("customer or competitor material list", prompt_text)
        self.assertIn("Competitor takeoff with shorthand notes", prompt_text)

    def test_context_interpreter_falls_back_when_model_returns_unknown_line_ids(self):
        class FakeResponses:
            def create(self, **kwargs):
                class Response:
                    output_text = '{"contextualized_lines":[{"line_id":"UNKNOWN","raw_text":"bad"}]}'
                return Response()

        class FakeClient:
            def __init__(self, api_key):
                self.responses = FakeResponses()

        interpreter = ContextInterpreter(api_key="test", model="gpt-4o")
        with patch("app.services.context_interpreter.OpenAI", FakeClient):
            contextualized = interpreter.interpret(
                [RawExtractedLine(line_id="L1", raw_text="12 boards", section_header="Trex", quantity=12)]
            )

        self.assertEqual(contextualized[0].line_id, "L1")
        self.assertIn("12 boards", contextualized[0].normalized_description)

    def test_context_interpreter_flags_handwritten_uncertainty(self):
        interpreter = ContextInterpreter(api_key="", model="gpt-4o")
        contextualized = interpreter.interpret(
            [RawExtractedLine(line_id="L1", raw_text="master entrance 3/0 (R) 4/9 swng surfce", quantity=1)]
        )

        self.assertIn("ocr_spelling_uncertain", contextualized[0].ambiguity_flags)
        self.assertIn("single_letter_annotation", contextualized[0].ambiguity_flags)
        self.assertIn("swinging", contextualized[0].normalized_description)

    def test_vision_extract_service_heic_uses_jpeg_payload(self):
        class FakeImage:
            def convert(self, _mode):
                return self

            def save(self, buffer, format, quality):
                buffer.write(b"jpeg-bytes")

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        service = VisionExtractService(api_key="test", model="gpt-4o")
        with TemporaryDirectory() as tmpdir, patch("app.services.vision_extract_service.Image.open", return_value=FakeImage()):
            file_path = Path(tmpdir) / "sample.heic"
            file_path.write_bytes(b"heic")
            payload = service._build_content(file_path)

        self.assertIn("data:image/jpeg;base64,", payload["image_url"])

    def test_vision_extract_service_retries_per_file_when_batch_file_index_missing(self):
        captured_inputs = []

        class FakeResponses:
            def __init__(self):
                self.calls = 0

            def create(self, **kwargs):
                self.calls += 1
                captured_inputs.append(kwargs["input"])

                class Response:
                    pass

                response = Response()
                if self.calls == 1:
                    response.output_text = (
                        '{"document_context":{"customer_name":"Smith"},'
                        '"lines":[{"line_id":"L1","raw_text":"first row"},{"line_id":"L2","raw_text":"second row"}]}'
                    )
                elif self.calls == 2:
                    response.output_text = '{"document_context":{},"lines":[{"line_id":"L1","raw_text":"file one row"}]}'
                else:
                    response.output_text = '{"document_context":{},"lines":[{"line_id":"L1","raw_text":"file two row"}]}'
                return response

        class FakeClient:
            def __init__(self, api_key):
                self.responses = FakeResponses()

        service = VisionExtractService(api_key="test", model="gpt-4o")
        with TemporaryDirectory() as tmpdir, patch("app.services.vision_extract_service.OpenAI", FakeClient):
            first = Path(tmpdir) / "one.jpg"
            second = Path(tmpdir) / "two.jpg"
            first.write_bytes(b"jpg")
            second.write_bytes(b"jpg")

            payload = service.extract_document([first, second], upload_context="Shared notes")

        self.assertEqual(
            [line["file_index"] for line in payload["lines"]],
            [1, 2],
        )
        self.assertEqual(payload["document_context"]["customer_name"], "Smith")
        self.assertEqual(len(captured_inputs), 3)


if __name__ == "__main__":
    unittest.main()
