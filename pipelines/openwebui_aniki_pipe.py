"""
title: Aniki CMM Neon Model
author: Local
description: 在 Open WebUI 中以「模型」形式接收 CMM PDF，呼叫 Windows 上的 Aniki API，解析後寫入 Neon。
required_open_webui_version: 0.9.0
requirements: requests
version: 1.0.0
license: MIT
"""

import asyncio
from pathlib import Path
from typing import Optional

import requests
from pydantic import BaseModel, Field


class Pipe:
    class Valves(BaseModel):
        API_URL: str = Field(
            default="http://host.docker.internal:8000",
            description="Windows 上 Aniki API 的網址",
        )
        API_KEY: str = Field(
            default="",
            description="必須與 TEST.PRO/.env 裡的 ANIKI_API_KEY 相同",
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
    def _resolve_uploaded_path(
        file_id: str,
        filename: str,
    ) -> Path:
        """
        Open WebUI Docker 預設附件位置：
        /app/backend/data/uploads/{file_id}_{filename}
        """

        path = Path(
            f"/app/backend/data/uploads/{file_id}_{filename}"
        )

        if not path.is_file():
            raise FileNotFoundError(
                f"找不到 Open WebUI 上傳檔案：{path}"
            )

        return path

    async def pipe(
        self,
        body: dict,
        __files__: Optional[list[dict]] = None,
        __metadata__: Optional[dict] = None,
        __event_emitter__=None,
        __task__: Optional[str] = None,
    ) -> str:
        """
        這個 Pipe 會直接顯示成 Open WebUI 的模型。

        使用方式：
        1. 選取本模型。
        2. 上傳 PDF。
        3. 輸入「解析並寫入 Neon」。
        """

        # 避免標題生成等背景任務誤觸發匯入流程
        if __task__:
            return "Aniki CMM Neon"

        files = __files__ or []
        if not files and __metadata__:
            files = __metadata__.get("files") or []

        if not files:
            return (
                "請先上傳 CMM PDF 或圖片，再傳送訊息。"
                "\n\n我會解析附件並將結果寫入 Neon。"
            )

        if not self.valves.API_KEY:
            return (
                "尚未設定 API_KEY。"
                "\n請到這個 Pipe 的 Valves，填入與 "
                "TEST.PRO/.env 中 ANIKI_API_KEY 相同的值。"
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

                uploaded_path = self._resolve_uploaded_path(
                    file_id,
                    filename,
                )

                def call_aniki_api() -> dict:
                    with uploaded_path.open("rb") as uploaded_file:
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
                            },
                            timeout=self.valves.TIMEOUT_SECONDS,
                        )

                    try:
                        payload = response.json()
                    except Exception:
                        payload = {
                            "detail": response.text,
                        }

                    if not response.ok:
                        raise RuntimeError(
                            f"Aniki API {response.status_code}: {payload}"
                        )

                    return payload

                payload = await asyncio.to_thread(call_aniki_api)

                metadata = payload.get("metadata") or {}
                row_count = payload.get("row_count", 0)
                page_numbers = payload.get("page_numbers") or []

                results.append(
                    "\n".join(
                        [
                            f"✅ **{filename}** 已完成並寫入 Neon",
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
                                "description": f"{filename} 已寫入 Neon",
                                "done": True,
                            },
                        }
                    )

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
