import base64
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

try:
    import requests  # noqa: F401
except ModuleNotFoundError:
    requests_stub = types.ModuleType("requests")
    requests_stub.post = Mock()
    sys.modules["requests"] = requests_stub

from openwebui_aniki_pipe import Pipe


class PipeTests(unittest.IsolatedAsyncioTestCase):
    def test_prefers_openwebui_supplied_file_path(self):
        with tempfile.TemporaryDirectory() as directory:
            upload_dir = Path(directory)
            uploaded = upload_dir / "stored-report.pdf"
            uploaded.write_bytes(b"pdf")
            item = {
                "file": {
                    "id": "file-id",
                    "filename": "report.pdf",
                    "path": str(uploaded),
                }
            }

            with patch.dict(
                os.environ,
                {"OPENWEBUI_UPLOAD_DIR": str(upload_dir)},
            ):
                resolved = Pipe._resolve_uploaded_path(
                    item,
                    "file-id",
                    "report.pdf",
                )

            self.assertEqual(resolved, uploaded.resolve())

    def test_extracts_inline_image_from_latest_user_message(self):
        image_bytes = b"fake-png-data"
        encoded = base64.b64encode(image_bytes).decode("ascii")
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": ""},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{encoded}",
                            },
                        },
                    ],
                }
            ]
        }

        images = Pipe._extract_inline_images(body)

        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["_aniki_inline_bytes"], image_bytes)
        self.assertTrue(images[0]["name"].endswith(".png"))

    async def test_send_with_attachment_immediately_calls_aniki(self):
        with tempfile.TemporaryDirectory() as directory:
            upload_dir = Path(directory)
            uploaded = upload_dir / "stored-report.pdf"
            uploaded.write_bytes(b"pdf")
            item = {
                "file": {
                    "id": "file-id",
                    "filename": "report.pdf",
                    "path": str(uploaded),
                }
            }
            response = Mock()
            response.ok = True
            response.status_code = 200
            response.json.return_value = {
                "metadata": {"品號": "P-1"},
                "row_count": 3,
                "page_numbers": [1],
            }

            pipe = Pipe()
            pipe.valves.API_KEY = "test-key"
            pipe.valves.API_URL = "http://pipelines:8000"

            with (
                patch.dict(
                    os.environ,
                    {"OPENWEBUI_UPLOAD_DIR": str(upload_dir)},
                ),
                patch(
                    "openwebui_aniki_pipe.requests.post",
                    return_value=response,
                ) as post,
            ):
                result = await pipe.pipe(
                    {"messages": [{"role": "user", "content": ""}]},
                    __files__=[item],
                )

            self.assertIn("已完成辨識", result)
            post.assert_called_once()

    async def test_duplicate_attachment_is_only_processed_once(self):
        with tempfile.TemporaryDirectory() as directory:
            upload_dir = Path(directory)
            uploaded = upload_dir / "stored-report.pdf"
            uploaded.write_bytes(b"pdf")
            item = {
                "file": {
                    "id": "same-file-id",
                    "filename": "report.pdf",
                    "path": str(uploaded),
                }
            }
            response = Mock()
            response.ok = True
            response.status_code = 200
            response.json.return_value = {
                "metadata": {"品號": "P-1"},
                "row_count": 3,
                "page_numbers": [1],
            }

            pipe = Pipe()
            pipe.valves.API_KEY = "test-key"

            with (
                patch.dict(
                    os.environ,
                    {"OPENWEBUI_UPLOAD_DIR": str(upload_dir)},
                ),
                patch(
                    "openwebui_aniki_pipe.requests.post",
                    return_value=response,
                ) as post,
            ):
                result = await pipe.pipe(
                    {"messages": [{"role": "user", "content": ""}]},
                    __files__=[item, dict(item)],
                )

            self.assertEqual(result.count("已完成辨識"), 1)
            post.assert_called_once()

    async def test_existing_file_asks_then_text_confirmation_replaces_it(self):
        with tempfile.TemporaryDirectory() as directory:
            upload_dir = Path(directory)
            uploaded = upload_dir / "stored-report.pdf"
            uploaded.write_bytes(b"pdf")
            item = {
                "file": {
                    "id": "file-id",
                    "filename": "report.pdf",
                    "path": str(uploaded),
                }
            }
            existing_response = Mock()
            existing_response.ok = False
            existing_response.status_code = 409
            existing_response.json.return_value = {
                "success": False,
                "error": "file_already_exists",
                "exists": True,
            }
            replaced_response = Mock()
            replaced_response.ok = True
            replaced_response.status_code = 200
            replaced_response.json.return_value = {
                "metadata": {"品號": "P-1"},
                "row_count": 3,
                "page_numbers": [1],
                "replaced_existing": True,
            }

            pipe = Pipe()
            pipe.valves.API_KEY = "test-key"

            with (
                patch.dict(
                    os.environ,
                    {"OPENWEBUI_UPLOAD_DIR": str(upload_dir)},
                ),
                patch(
                    "openwebui_aniki_pipe.requests.post",
                    side_effect=[existing_response, replaced_response],
                ) as post,
            ):
                question = await pipe.pipe(
                    {"messages": [{"role": "user", "content": ""}]},
                    __files__=[item],
                    __metadata__={"chat_id": "chat-1"},
                )
                result = await pipe.pipe(
                    {
                        "messages": [
                            {"role": "user", "content": "取代"}
                        ]
                    },
                    __metadata__={"chat_id": "chat-1"},
                )

            self.assertIn("是否用這次上傳的新資料取代", question)
            self.assertIn("已完成辨識並取代", result)
            self.assertNotIn("處理失敗", result)
            self.assertEqual(post.call_count, 2)
            self.assertEqual(
                post.call_args_list[1].kwargs["data"]["replace_existing"],
                "true",
            )

    async def test_confirmation_dialog_can_replace_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            upload_dir = Path(directory)
            uploaded = upload_dir / "stored-report.pdf"
            uploaded.write_bytes(b"new-pdf")
            item = {
                "file": {
                    "id": "file-id",
                    "filename": "report.pdf",
                    "path": str(uploaded),
                }
            }
            existing_response = Mock()
            existing_response.ok = False
            existing_response.status_code = 409
            existing_response.json.return_value = {
                "error": "file_already_exists",
                "exists": True,
            }
            replaced_response = Mock()
            replaced_response.ok = True
            replaced_response.status_code = 200
            replaced_response.json.return_value = {
                "metadata": {"品號": "P-1"},
                "row_count": 4,
                "page_numbers": [1],
                "replaced_existing": True,
            }

            async def confirm(_event):
                return True

            pipe = Pipe()
            pipe.valves.API_KEY = "test-key"

            with (
                patch.dict(
                    os.environ,
                    {"OPENWEBUI_UPLOAD_DIR": str(upload_dir)},
                ),
                patch(
                    "openwebui_aniki_pipe.requests.post",
                    side_effect=[existing_response, replaced_response],
                ) as post,
            ):
                result = await pipe.pipe(
                    {"messages": [{"role": "user", "content": ""}]},
                    __files__=[item],
                    __event_call__=confirm,
                )

            self.assertIn("已完成辨識並取代", result)
            self.assertEqual(post.call_count, 2)
            self.assertEqual(
                post.call_args_list[1].kwargs["data"]["replace_existing"],
                "true",
            )


if __name__ == "__main__":
    unittest.main()
