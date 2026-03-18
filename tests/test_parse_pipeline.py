import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

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
            def extract(self, _file_path):
                return [
                    {"line_id": "L1", "raw_text": "first row"},
                    {"line_id": "L2", "raw_text": "second row"},
                ]

        lines = stage_a_extract(
            [Path("one.pdf"), Path("two.pdf")],
            StubVisionService(),
        )

        self.assertEqual(
            [line.line_id for line in lines],
            ["F1-L1", "F1-L2", "F2-L1", "F2-L2"],
        )

    def test_vision_extract_service_uses_webp_mime(self):
        with TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "sample.webp"
            file_path.write_bytes(b"RIFF0000WEBPVP8 ")

            service = VisionExtractService(api_key="test", model="gpt-4o")
            payload = service._build_content(file_path)

        self.assertIn("data:image/webp;base64,", payload["image_url"])


if __name__ == "__main__":
    unittest.main()
