"""
title: Aniki
author: Local
description: 上傳 CMM PDF 或圖片並送出後，自動辨識並寫入本地 PostgreSQL。
required_open_webui_version: 0.9.0
requirements: requests
version: 1.3.0
license: MIT
"""

import asyncio
import base64
import binascii
import hashlib
import io
import os
from pathlib import Path
from typing import Optional

import requests
from pydantic import BaseModel, Field


class Pipe:
    class Valves(BaseModel):
        API_URL: str = Field(
            default=os.getenv(
                "ANIKI_API_URL",
                "http://pipelines:8000",
            ),
            description="Aniki API 的網址",
        )
        API_KEY: str = Field(
            default=os.getenv(
                "ANIKI_API_KEY",
                "",
            ),
            description="必須與專案根目錄 .env 的 ANIKI_API_KEY 相同",
        )
        TIMEOUT_SECONDS: int = Field(
            default=7200,
            description="等待 Aniki 完成的最長秒數",
        )
        DEFAULT_PAGES: str = Field(
            default="",
            description="留空表示 PDF 全部頁面；也可填 1 或 1,2,3",
        )

    def __init__(self):
        self.valves = self.Valves()
        self._pending_replacements: dict[str, list[dict]] = {}

    @staticmethod
    def _latest_user_text(body: dict) -> str:
        messages = body.get("messages") or []
        last_user_message = next(
            (
                message
                for message in reversed(messages)
                if message.get("role") == "user"
            ),
            None,
        )
        if not last_user_message:
            return ""

        content = last_user_message.get("content", "")
        if isinstance(content, str):
            return content.strip()
        if not isinstance(content, list):
            return ""

        return " ".join(
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ).strip()

    @classmethod
    def _replacement_decision(cls, body: dict) -> Optional[bool]:
        text = "".join(cls._latest_user_text(body).lower().split())
        if not text:
            return None

        negative_phrases = (
            "不要",
            "不取代",
            "不覆蓋",
            "保留舊資料",
        )
        if (
            text in {"取消", "否", "no", "cancel"}
            or any(word in text for word in negative_phrases)
        ):
            return False

        positive_phrases = (
            "更新舊資料",
            "確認更新",
        )
        if (
            text in {"取代", "覆蓋", "確認", "是", "yes", "ok"}
            or "取代" in text
            or "覆蓋" in text
            or any(word in text for word in positive_phrases)
        ):
            return True

        return None

    @staticmethod
    def _context_key(
        metadata: Optional[dict],
        user: Optional[dict],
    ) -> str:
        metadata = metadata or {}
        user = user or {}

        for key in ("chat_id", "conversation_id", "session_id"):
            value = metadata.get(key)
            if value:
                return f"chat:{value}"

        user_id = user.get("id") or user.get("email")
        if user_id:
            return f"user:{user_id}"

        # 本專案是本機單使用者部署；無注入資訊時使用固定範圍。
        return "local-default"

    @staticmethod
    def _confirmation_accepted(response) -> bool:
        if isinstance(response, bool):
            return response
        if not isinstance(response, dict):
            return False

        for key in ("confirmed", "result", "value", "accepted"):
            value = response.get(key)
            if isinstance(value, bool):
                return value

        data = response.get("data")
        if isinstance(data, dict):
            return Pipe._confirmation_accepted(data)

        return False

    @staticmethod
    def _extract_file_info(item: dict) -> tuple[str, str]:
        file_data = item.get("file") or item.get("files") or {}

        file_id = (
            file_data.get("id")
            or item.get("id")
        )
        filename = (
            file_data.get("filename")
            or item.get("name")
            or ""
        )

        if not file_id:
            raise ValueError("附件中找不到 file id")

        if not filename:
            raise ValueError("附件中找不到原始檔名")

        return str(file_id), Path(str(filename)).name

    @staticmethod
    def _extract_inline_images(body: dict) -> list[dict]:
        """Convert images from the latest user message into upload items."""
        messages = body.get("messages") or []
        last_user_message = next(
            (
                message
                for message in reversed(messages)
                if message.get("role") == "user"
            ),
            None,
        )
        if not last_user_message:
            return []

        content = last_user_message.get("content")
        if not isinstance(content, list):
            return []

        extension_by_mime = {
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
        }
        images: list[dict] = []

        for content_item in content:
            if (
                not isinstance(content_item, dict)
                or content_item.get("type") != "image_url"
            ):
                continue

            image_url = content_item.get("image_url") or {}
            url = (
                image_url.get("url", "")
                if isinstance(image_url, dict)
                else str(image_url)
            )
            if not url.startswith("data:image/") or "," not in url:
                continue

            header, encoded = url.split(",", 1)
            mime_type = header[5:].split(";", 1)[0].lower()
            extension = extension_by_mime.get(mime_type)
            if not extension or ";base64" not in header.lower():
                continue

            try:
                image_bytes = base64.b64decode(
                    encoded,
                    validate=True,
                )
            except (ValueError, binascii.Error):
                continue

            if not image_bytes:
                continue

            digest = hashlib.sha256(image_bytes).hexdigest()[:12]
            supplied_name = content_item.get("name")
            filename = (
                Path(str(supplied_name)).name
                if supplied_name
                else f"image-{digest}{extension}"
            )
            if Path(filename).suffix.lower() not in extension_by_mime.values():
                filename += extension

            images.append(
                {
                    "name": filename,
                    "_aniki_inline_bytes": image_bytes,
                }
            )

        return images

    @staticmethod
    def _deduplicate_files(files: list[dict]) -> list[dict]:
        """Remove duplicate attachment records emitted by Open WebUI."""
        unique_files: list[dict] = []
        seen_tokens: set[tuple[str, str]] = set()

        for item in files:
            file_data = item.get("file") or item.get("files") or {}
            tokens: set[tuple[str, str]] = set()

            inline_bytes = item.get("_aniki_inline_bytes")
            if inline_bytes is not None:
                tokens.add(
                    (
                        "content",
                        hashlib.sha256(inline_bytes).hexdigest(),
                    )
                )

            file_id = file_data.get("id") or item.get("id")
            if file_id:
                tokens.add(("id", str(file_id)))

            supplied_path = file_data.get("path") or item.get("path")
            if supplied_path:
                tokens.add(("path", str(supplied_path)))

            filename = (
                file_data.get("filename")
                or item.get("name")
                or ""
            )
            if filename:
                # Aniki 的資料庫也是以原始檔名阻擋重複匯入。
                tokens.add(("name", Path(str(filename)).name.casefold()))

            if tokens and tokens.intersection(seen_tokens):
                continue

            unique_files.append(item)
            seen_tokens.update(tokens)

        return unique_files

    @staticmethod
    def _resolve_uploaded_path(
        item: dict,
        file_id: str,
        filename: str,
    ) -> Path:
        """
        新版 Open WebUI 會直接在 __files__ 提供 file.path；舊版則使用
        /app/backend/data/uploads/{file_id}_{filename}。兩種格式都支援。
        """
        file_data = item.get("file") or {}
        supplied_path = (
            file_data.get("path")
            or item.get("path")
        )
        upload_dir = Path(
            os.getenv(
                "OPENWEBUI_UPLOAD_DIR",
                "/app/backend/data/uploads",
            )
        ).resolve()

        candidates: list[Path] = []
        if supplied_path:
            candidates.append(Path(str(supplied_path)))

        candidates.append(
            upload_dir / f"{file_id}_{filename}"
        )

        # 部分 Open WebUI 版本會調整原始檔名，但仍保留 file id 前綴。
        candidates.extend(
            upload_dir.glob(f"{file_id}_*")
        )

        for candidate in candidates:
            try:
                resolved = candidate.resolve()
                resolved.relative_to(upload_dir)
            except (OSError, ValueError):
                continue

            if resolved.is_file():
                return resolved

        raise FileNotFoundError(
            f"找不到 Open WebUI 上傳檔案：{filename} ({file_id})"
        )

    async def pipe(
        self,
        body: dict,
        __files__: Optional[list[dict]] = None,
        __metadata__: Optional[dict] = None,
        __user__: Optional[dict] = None,
        __event_emitter__=None,
        __event_call__=None,
        __task__: Optional[str] = None,
    ) -> str:
        """
        這個 Pipe 會直接顯示成 Open WebUI 的模型。

        Aniki 是預設模型。使用者上傳檔案並按送出後，立即開始辨識，
        不需要輸入特定指令。
        """

        # 避免標題生成等背景任務誤觸發匯入流程
        if __task__:
            return "Aniki"

        context_key = self._context_key(__metadata__, __user__)
        replacement_decision = self._replacement_decision(body)

        if replacement_decision is False:
            self._pending_replacements.pop(context_key, None)
            return "已取消取代，資料庫中的舊資料會保留。"

        files = list(__files__ or [])
        if not files and __metadata__:
            files = list(__metadata__.get("files") or [])

        # Open WebUI 將圖片放在訊息的 image_url，而不是 __files__。
        files.extend(self._extract_inline_images(body))
        files = self._deduplicate_files(files)

        replacing_existing = replacement_decision is True
        if not files and replacing_existing:
            files = list(
                self._pending_replacements.get(context_key) or []
            )

        if files and not replacing_existing:
            self._pending_replacements.pop(context_key, None)

        if not files:
            return (
                "請先上傳 CMM PDF 或圖片，再傳送訊息。"
                "\n\n附件送出後會自動辨識並寫入資料庫。"
            )

        if not self.valves.API_KEY:
            return (
                "尚未設定 API_KEY。"
                "\n請檢查 Docker Compose 是否已把 "
                "ANIKI_API_KEY 傳入 Open WebUI。"
            )

        supported_extensions = {
            ".pdf",
            ".png",
            ".jpg",
            ".jpeg",
            ".webp",
        }

        results: list[str] = []

        for item in files:
            display_name = (
                item.get("name")
                or (item.get("file") or {}).get("filename")
                or "附件"
            )

            try:
                inline_bytes = item.get("_aniki_inline_bytes")
                if inline_bytes is not None:
                    filename = Path(
                        str(item.get("name") or "image.png")
                    ).name
                    file_id = ""
                else:
                    file_id, filename = self._extract_file_info(item)
                extension = Path(filename).suffix.lower()

                if extension not in supported_extensions:
                    results.append(
                        f"⚠️ **{filename}**：不支援此檔案格式。"
                    )
                    continue

                if __event_emitter__:
                    await __event_emitter__(
                        {
                            "type": "status",
                            "data": {
                                "description": f"正在解析 {filename}…",
                                "done": False,
                            },
                        }
                    )

                uploaded_path = (
                    None
                    if inline_bytes is not None
                    else self._resolve_uploaded_path(
                        item,
                        file_id,
                        filename,
                    )
                )

                def call_aniki_api(replace_existing: bool = False) -> dict:
                    uploaded_file = (
                        io.BytesIO(inline_bytes)
                        if inline_bytes is not None
                        else uploaded_path.open("rb")
                    )
                    with uploaded_file:
                        response = requests.post(
                            self.valves.API_URL.rstrip("/") + "/process",
                            headers={
                                "X-API-Key": self.valves.API_KEY,
                            },
                            files={
                                "file": (
                                    filename,
                                    uploaded_file,
                                    "application/octet-stream",
                                )
                            },
                            data={
                                "pages": self.valves.DEFAULT_PAGES,
                                "replace_existing": (
                                    "true" if replace_existing else "false"
                                ),
                            },
                            timeout=self.valves.TIMEOUT_SECONDS,
                        )

                    try:
                        payload = response.json()
                    except Exception:
                        payload = {
                            "detail": response.text,
                        }

                    if (
                        response.status_code == 409
                        and payload.get("error") == "file_already_exists"
                    ):
                        payload["_aniki_existing"] = True
                        return payload

                    if not response.ok:
                        raise RuntimeError(
                            f"Aniki API {response.status_code}: {payload}"
                        )

                    return payload

                confirmed = False
                payload = await asyncio.to_thread(
                    call_aniki_api,
                    replacing_existing,
                )

                if payload.get("_aniki_existing"):
                    if __event_call__:
                        confirmation = await __event_call__(
                            {
                                "type": "confirmation",
                                "data": {
                                    "title": "檔案已存在",
                                    "message": (
                                        f"{filename} 已存在資料庫。"
                                        "是否用這次上傳的新資料取代舊資料？"
                                    ),
                                },
                            }
                        )
                        confirmed = self._confirmation_accepted(
                            confirmation
                        )

                    if confirmed:
                        if __event_emitter__:
                            await __event_emitter__(
                                {
                                    "type": "status",
                                    "data": {
                                        "description": (
                                            f"正在以新資料取代 {filename}…"
                                        ),
                                        "done": False,
                                    },
                                }
                            )
                        payload = await asyncio.to_thread(
                            call_aniki_api,
                            True,
                        )
                    elif __event_call__:
                        results.append(
                            f"ℹ️ **{filename}**：已取消取代，"
                            "舊資料保持不變。"
                        )
                        if __event_emitter__:
                            await __event_emitter__(
                                {
                                    "type": "status",
                                    "data": {
                                        "description": (
                                            f"{filename} 已取消取代"
                                        ),
                                        "done": True,
                                    },
                                }
                            )
                        continue
                    else:
                        self._pending_replacements.setdefault(
                            context_key,
                            [],
                        ).append(item)
                        results.append(
                            "\n".join(
                                [
                                    f"⚠️ **{filename}** 已存在資料庫。",
                                    "是否用這次上傳的新資料取代舊資料？",
                                    "請回覆 **取代** 或 **取消**。",
                                ]
                            )
                        )
                        if __event_emitter__:
                            await __event_emitter__(
                                {
                                    "type": "status",
                                    "data": {
                                        "description": (
                                            f"{filename} 等待確認是否取代"
                                        ),
                                        "done": True,
                                    },
                                }
                            )
                        continue

                metadata = payload.get("metadata") or {}
                row_count = payload.get("row_count", 0)
                page_numbers = payload.get("page_numbers") or []

                completion_text = (
                    "已完成辨識並取代資料庫中的舊資料"
                    if payload.get("replaced_existing")
                    or replacing_existing
                    or confirmed
                    else "已完成辨識並寫入資料庫"
                )
                results.append(
                    "\n".join(
                        [
                            f"✅ **{filename}** {completion_text}",
                            f"- 品號：{metadata.get('品號') or '未讀到'}",
                            f"- 製程：{metadata.get('製程') or '未讀到'}",
                            f"- 流水號：{metadata.get('流水號') or '未讀到'}",
                            f"- 處理頁面：{page_numbers}",
                            f"- 測量項目：{row_count} 筆",
                        ]
                    )
                )

                if __event_emitter__:
                    await __event_emitter__(
                        {
                            "type": "status",
                            "data": {
                                "description": f"{filename} 已寫入資料庫",
                                "done": True,
                            },
                        }
                    )

                self._pending_replacements.pop(context_key, None)

            except Exception as error:
                results.append(
                    f"❌ **{display_name}** 處理失敗："
                    f"{type(error).__name__}: {error}"
                )

                if __event_emitter__:
                    await __event_emitter__(
                        {
                            "type": "status",
                            "data": {
                                "description": f"{display_name} 處理失敗",
                                "done": True,
                            },
                        }
                    )

        return "\n\n".join(results)
