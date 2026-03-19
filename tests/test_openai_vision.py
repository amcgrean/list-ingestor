import importlib.util
import json
import sys
import unittest
from pathlib import Path


_SERVICE_PATH = Path(__file__).parent.parent / "services" / "openai_vision.py"
_spec = importlib.util.spec_from_file_location("openai_vision", _SERVICE_PATH)
openai_vision = importlib.util.module_from_spec(_spec)
try:
    _spec.loader.exec_module(openai_vision)
except ModuleNotFoundError as exc:
    if exc.name == "openai":
        raise unittest.SkipTest("OpenAI SDK is required for the vision service tests")
    raise

sys.modules.setdefault("openai_vision", openai_vision)


class OpenAIVisionParsingTests(unittest.TestCase):
    def test_prompt_includes_upload_context_and_multi_file_scope(self):
        prompt = openai_vision._build_system_prompt(
            upload_context="Customer list with handwritten notes",
            file_count=2,
        )

        self.assertIn("customer or competitor material list", prompt)
        self.assertIn("Treat all provided files as one related document set", prompt)
        self.assertIn("Customer list with handwritten notes", prompt)

    def test_parses_structured_context_payload(self):
        payload = {
            "document_context": {
                "summary": "Trex order for the Smith patio job",
                "customer_name": "Smith",
                "project_name": "Patio refresh",
                "global_material_context": ["Trex Toasted Sand"],
                "job_notes": ["Deliver to rear gate"],
                "warnings": ["Color applies to decking rows only"],
            },
            "items": [
                {
                    "quantity": 12,
                    "description": "Trex Toasted Sand deck board 12ft",
                    "source_description": "12ft boards",
                    "applied_context": ["Trex Toasted Sand"],
                }
            ],
        }

        result = openai_vision._parse_json_response(json.dumps(payload))

        self.assertEqual(
            result["document_context"]["global_material_context"],
            ["Trex Toasted Sand"],
        )
        self.assertEqual(result["document_context"]["customer_name"], "Smith")
        self.assertEqual(result["items"][0]["source_description"], "12ft boards")
        self.assertEqual(result["items"][0]["quantity"], 12.0)

    def test_legacy_array_payload_still_parses(self):
        result = openai_vision._parse_json_response(
            json.dumps([{"quantity": 2, "description": "2x10 joists 16ft"}])
        )

        self.assertEqual(result["document_context"]["summary"], "")
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["description"], "2x10 joists 16ft")
        self.assertEqual(result["items"][0]["source_description"], "2x10 joists 16ft")


if __name__ == "__main__":
    unittest.main()
