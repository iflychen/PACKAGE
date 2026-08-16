import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from app import router as review_router
import psycopg
from dotenv import load_dotenv
from fastapi import (
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.responses import JSONResponse

from neon_db import get_database_url

try:
    import pymupdf
except ImportError:
    import fitz as pymupdf


BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
ANIKI_SCRIPT = BASE_DIR / "Aniki.py"
RESULT_PATH = BASE_DIR / "all_pages_result.json"
UPLOAD_DIR = BASE_DIR

load_dotenv(ENV_PATH, override=True)

ANIKI_API_KEY = os.getenv(
    "ANIKI_API_KEY",
    "",
).strip()

ALLOWED_EXTENSIONS = {
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
}

PROCESS_TIMEOUT_SECONDS = 7200
DUPLICATE_WINDOW_SECONDS = 600

PROCESS_LOCK = threading.Lock()

RECENT_SUCCESS_RESULTS: dict[
    str,
    tuple[float, dict[str, Any]],
] = {}

app = FastAPI(
    title="Aniki Report API",
    version="2.1.0",
    description=(
        "接收 Open WebUI 上傳的單一或多個檔案，"
        "呼叫 Aniki.py，並將結果寫入 Neon。"
    ),
)
app.include_router(review_router)

def verify_api_key(
    x_api_key: Optional[str],
) -> None:
    if not ANIKI_API_KEY:
        raise RuntimeError(
            f"尚未設定 ANIKI_API_KEY，請檢查：{ENV_PATH}"
        )

    if x_api_key != ANIKI_API_KEY:
        raise HTTPException(
            status_code=401,
            detail="API key 錯誤",
        )


def format_database_time(
    value: Any,
) -> Optional[str]:
    if value is None:
        return None

    if isinstance(value, datetime):
        return value.isoformat(
            sep=" ",
            timespec="seconds",
        )

    return str(value)


def lookup_existing_file(
    file_name: str,
) -> dict[str, Any]:
    with psycopg.connect(
        get_database_url(),
        connect_timeout=15,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    "檔名",
                    "上傳時間",
                    "路徑"
                FROM public."來源檔案"
                WHERE "檔名" = %s
                LIMIT 1;
                """,
                (file_name,),
            )

            row = cursor.fetchone()

    if row is None:
        return {
            "exists": False,
            "file_name": file_name,
            "uploaded_at": None,
            "path": None,
        }

    return {
        "exists": True,
        "file_name": str(row[0]),
        "uploaded_at": format_database_time(
            row[1]
        ),
        "path": (
            None
            if row[2] is None
            else str(row[2])
        ),
    }


def parse_page_numbers(
    file_path: Path,
    pages: Optional[str],
) -> list[int]:
    if pages and pages.strip():
        normalized = pages.replace(
            ",",
            " ",
        )

        try:
            page_numbers = [
                int(value)
                for value in normalized.split()
            ]
        except ValueError as error:
            raise ValueError(
                f"頁碼格式錯誤：{pages}"
            ) from error

        if not page_numbers:
            raise ValueError(
                "沒有合法頁碼"
            )

        if any(
            page < 1
            for page in page_numbers
        ):
            raise ValueError(
                "頁碼必須大於或等於 1"
            )

        return page_numbers

    if file_path.suffix.lower() != ".pdf":
        return [1]

    document = pymupdf.open(
        file_path
    )

    try:
        if len(document) < 1:
            raise ValueError(
                "PDF 裡沒有可讀取的頁面"
            )

        return list(
            range(
                1,
                len(document) + 1,
            )
        )

    finally:
        document.close()


def calculate_file_sha256(
    file_path: Path,
) -> str:
    sha256 = hashlib.sha256()

    with file_path.open("rb") as source:
        while True:
            chunk = source.read(
                1024 * 1024
            )

            if not chunk:
                break

            sha256.update(chunk)

    return sha256.hexdigest()


def build_content_key(
    file_path: Path,
    page_numbers: list[int],
) -> str:
    pages_text = ",".join(
        str(page)
        for page in page_numbers
    )

    return (
        f"{calculate_file_sha256(file_path)}:"
        f"{pages_text}"
    )


def cleanup_result_cache(
    now: float,
) -> None:
    expired_keys = [
        key
        for key, (
            finished_at,
            _,
        ) in RECENT_SUCCESS_RESULTS.items()
        if (
            now - finished_at
            > DUPLICATE_WINDOW_SECONDS
        )
    ]

    for key in expired_keys:
        RECENT_SUCCESS_RESULTS.pop(
            key,
            None,
        )


def read_result_summary() -> dict[str, Any]:
    if not RESULT_PATH.is_file():
        return {
            "metadata": {},
            "pages": [],
            "row_count": 0,
        }

    with RESULT_PATH.open(
        "r",
        encoding="utf-8",
    ) as source:
        result = json.load(source)

    metadata = (
        result.get("metadata")
        or {}
    )

    pages = (
        result.get("pages")
        or []
    )

    row_count = sum(
        len(
            page.get("rows")
            or []
        )
        for page in pages
        if isinstance(
            page,
            dict,
        )
    )

    return {
        "metadata": metadata,
        "pages": pages,
        "row_count": row_count,
    }


def run_aniki_process(
    command: list[str],
    process_environment: dict[str, str],
) -> tuple[int, str]:
    process = subprocess.Popen(
        command,
        cwd=BASE_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=process_environment,
    )

    if process.stdout is None:
        process.kill()

        raise RuntimeError(
            "無法讀取 Aniki.py 的輸出"
        )

    output_queue: queue.Queue = (
        queue.Queue()
    )

    def read_output() -> None:
        try:
            for line in process.stdout:
                output_queue.put(line)
        finally:
            output_queue.put(None)

    reader_thread = threading.Thread(
        target=read_output,
        daemon=True,
    )

    reader_thread.start()

    output_lines: list[str] = []
    reader_finished = False

    started_at = time.monotonic()

    while True:
        if (
            time.monotonic()
            - started_at
            > PROCESS_TIMEOUT_SECONDS
        ):
            process.kill()

            raise subprocess.TimeoutExpired(
                command,
                PROCESS_TIMEOUT_SECONDS,
            )

        try:
            item = output_queue.get(
                timeout=0.2
            )

            if item is None:
                reader_finished = True
            else:
                print(
                    f"[Aniki] {item}",
                    end="",
                    flush=True,
                )

                output_lines.append(item)

        except queue.Empty:
            pass

        if (
            process.poll() is not None
            and reader_finished
            and output_queue.empty()
        ):
            break

    return (
        process.wait(),
        "".join(output_lines),
    )


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "aniki_exists": (
            ANIKI_SCRIPT.is_file()
        ),
        "env_exists": (
            ENV_PATH.is_file()
        ),
        "api_key_configured": bool(
            ANIKI_API_KEY
        ),
    }


@app.get("/file-status")
def file_status(
    file_name: str = Query(...),
    x_api_key: Optional[str] = Header(
        default=None
    ),
) -> dict[str, Any]:
    verify_api_key(x_api_key)

    safe_name = Path(
        file_name
    ).name

    if not safe_name:
        raise HTTPException(
            status_code=400,
            detail="檔名不能為空",
        )

    return lookup_existing_file(
        safe_name
    )


def _process_single_report(
    file: UploadFile,
    pages: Optional[str],
    replace_existing: bool,
    request_id: Optional[str],
    x_api_key: Optional[str],
):
    """
    實際處理單一檔案。

    /process 收到多個檔案時，
    會重複呼叫這個函式。
    """

    verify_api_key(x_api_key)

    if not ANIKI_SCRIPT.is_file():
        raise HTTPException(
            status_code=500,
            detail=(
                f"找不到 Aniki.py："
                f"{ANIKI_SCRIPT}"
            ),
        )

    original_name = Path(
        file.filename or ""
    ).name

    if not original_name:
        raise HTTPException(
            status_code=400,
            detail="檔案沒有名稱",
        )

    extension = Path(
        original_name
    ).suffix.lower()

    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"不支援的格式："
                f"{extension}"
            ),
        )

    existing_file = lookup_existing_file(
        original_name
    )

    if (
        existing_file["exists"]
        and not replace_existing
    ):
        return JSONResponse(
            status_code=409,
            content={
                "success": False,
                "error": (
                    "file_already_exists"
                ),
                **existing_file,
            },
        )

    destination = (
        UPLOAD_DIR
        / original_name
    )

    temporary_path = (
        destination.with_suffix(
            destination.suffix
            + ".uploading"
        )
    )

    try:
        print(
            "\n"
            "========================================",
            flush=True,
        )

        print(
            "[API] 收到新的檔案處理請求",
            flush=True,
        )

        print(
            f"[API] 檔案名稱："
            f"{original_name}",
            flush=True,
        )

        print(
            f"[API] 取代既有資料："
            f"{replace_existing}",
            flush=True,
        )

        print(
            f"[API] Request ID："
            f"{request_id or '(無)'}",
            flush=True,
        )

        with temporary_path.open(
            "wb"
        ) as output:
            shutil.copyfileobj(
                file.file,
                output,
            )

        temporary_path.replace(
            destination
        )

        page_numbers = parse_page_numbers(
            destination,
            pages,
        )

        content_key = build_content_key(
            destination,
            page_numbers,
        )

        command = [
            sys.executable,
            str(ANIKI_SCRIPT),
            original_name,
            *[
                str(page)
                for page in page_numbers
            ],
        ]

        process_environment = (
            os.environ.copy()
        )

        process_environment[
            "PYTHONIOENCODING"
        ] = "utf-8"

        process_environment[
            "PYTHONUNBUFFERED"
        ] = "1"

        with PROCESS_LOCK:
            now = time.monotonic()

            cleanup_result_cache(now)

            cached_entry = (
                RECENT_SUCCESS_RESULTS.get(
                    content_key
                )
            )

            if cached_entry is not None:
                (
                    _,
                    cached_response,
                ) = cached_entry

                duplicate_response = dict(
                    cached_response
                )

                duplicate_response[
                    "duplicate_skipped"
                ] = True

                print(
                    "[API] 偵測到相同檔案的"
                    "重複請求",
                    flush=True,
                )

                print(
                    "[API] 不重新執行 VLM，"
                    "直接回傳上次成功結果",
                    flush=True,
                )

                return duplicate_response

            if RESULT_PATH.exists():
                RESULT_PATH.unlink()

            print(
                f"[API] 準備讀取頁面："
                f"{page_numbers}",
                flush=True,
            )

            print(
                "[API] Aniki.py 開始執行",
                flush=True,
            )

            (
                return_code,
                stdout,
            ) = run_aniki_process(
                command,
                process_environment,
            )

            summary = (
                read_result_summary()
            )

        neon_success = (
            "Neon 寫入成功" in stdout
            and
            "Neon 寫入失敗" not in stdout
        )

        success = (
            return_code == 0
            and RESULT_PATH.is_file()
        )

        response: dict[str, Any] = {
            "success": success,
            "duplicate_skipped": False,
            "replaced_existing": bool(
                existing_file["exists"]
                and replace_existing
            ),
            "previous_uploaded_at": (
                existing_file[
                    "uploaded_at"
                ]
            ),
            "file_name": original_name,
            "page_numbers": page_numbers,
            "metadata": (
                summary["metadata"]
            ),
            "pages": (
                summary["pages"]
            ),
            "row_count": (
                summary["row_count"]
            ),
            "neon_success": (
                neon_success
            ),
            "return_code": (
                return_code
            ),
            "stdout": (
                stdout[-12000:]
            ),
            "stderr": "",
        }

        if not success:
            print(
                "[API] 處理失敗",
                flush=True,
            )

            return JSONResponse(
                status_code=500,
                content=response,
            )

        RECENT_SUCCESS_RESULTS[
            content_key
        ] = (
            time.monotonic(),
            dict(response),
        )

        print(
            "[API] 處理完成，"
            "資料已成功寫入 Neon",
            flush=True,
        )

        print(
            f"[API] 測量項目數量："
            f"{summary['row_count']}",
            flush=True,
        )

        print(
            "========================================"
            "\n",
            flush=True,
        )

        return response

    except subprocess.TimeoutExpired as error:
        raise HTTPException(
            status_code=504,
            detail=(
                f"Aniki 執行超時："
                f"{error}"
            ),
        ) from error

    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error),
        ) from error

    except HTTPException:
        raise

    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=(
                f"處理失敗："
                f"{type(error).__name__}: "
                f"{error}"
            ),
        ) from error

    finally:
        try:
            file.file.close()
        except Exception:
            pass

        if temporary_path.exists():
            temporary_path.unlink(
                missing_ok=True
            )


@app.post("/process")
def process_reports(
    file: list[UploadFile] = File(...),
    pages: Optional[str] = Form(
        default=None
    ),
    replace_existing: bool = Form(
        default=False
    ),
    request_id: Optional[str] = Form(
        default=None
    ),
    x_api_key: Optional[str] = Header(
        default=None
    ),
):
    """
    /process 直接支援單一或多個檔案。

    前端傳送多個檔案時，
    每個檔案都必須使用相同的 file 欄位名稱。

    例如：

    file=@report1.pdf
    file=@report2.pdf
    file=@report3.png
    """

    verify_api_key(x_api_key)

    if not file:
        raise HTTPException(
            status_code=400,
            detail=(
                "至少要上傳一個檔案"
            ),
        )

    file_count = len(file)

    print(
        "\n"
        "========================================",
        flush=True,
    )

    print(
        f"[API] /process 一次收到 "
        f"{file_count} 個檔案",
        flush=True,
    )

    print(
        "========================================",
        flush=True,
    )

    # 只有一個檔案時，
    # 維持原本單檔回傳格式。
    if file_count == 1:
        return _process_single_report(
            file=file[0],
            pages=pages,
            replace_existing=(
                replace_existing
            ),
            request_id=request_id,
            x_api_key=x_api_key,
        )

    batch_started_at = (
        time.monotonic()
    )

    results: list[
        dict[str, Any]
    ] = []

    # 一次接收多個檔案，
    # 但依序執行 Aniki.py。
    #
    # 因為 Aniki.py 會共用：
    # all_pages_result.json
    #
    # 若同時平行處理，
    # 不同檔案的結果可能互相覆蓋。
    for index, upload_file in enumerate(
        file,
        start=1,
    ):
        file_name = Path(
            upload_file.filename
            or ""
        ).name

        child_request_id = (
            f"{request_id}-{index}"
            if request_id
            else f"batch-{index}"
        )

        print(
            f"[API] 正在處理 "
            f"{index}/{file_count}："
            f"{file_name or '(無檔名)'}",
            flush=True,
        )

        try:
            result = (
                _process_single_report(
                    file=upload_file,
                    pages=pages,
                    replace_existing=(
                        replace_existing
                    ),
                    request_id=(
                        child_request_id
                    ),
                    x_api_key=x_api_key,
                )
            )

            if isinstance(
                result,
                JSONResponse,
            ):
                payload = json.loads(
                    result.body.decode(
                        "utf-8"
                    )
                )

                payload[
                    "http_status"
                ] = result.status_code

            else:
                payload = dict(result)

                payload[
                    "http_status"
                ] = 200

            payload[
                "batch_index"
            ] = index

            results.append(payload)

        except HTTPException as error:
            results.append(
                {
                    "success": False,
                    "batch_index": index,
                    "file_name": (
                        file_name or None
                    ),
                    "http_status": (
                        error.status_code
                    ),
                    "error": error.detail,
                }
            )

        except Exception as error:
            results.append(
                {
                    "success": False,
                    "batch_index": index,
                    "file_name": (
                        file_name or None
                    ),
                    "http_status": 500,
                    "error": (
                        f"{type(error).__name__}: "
                        f"{error}"
                    ),
                }
            )

    success_count = sum(
        1
        for item in results
        if item.get("success") is True
    )

    failed_count = (
        len(results)
        - success_count
    )

    response = {
        "success": (
            failed_count == 0
        ),
        "total_count": (
            len(results)
        ),
        "success_count": (
            success_count
        ),
        "failed_count": (
            failed_count
        ),
        "elapsed_seconds": round(
            time.monotonic()
            - batch_started_at,
            2,
        ),
        "results": results,
    }

    print(
        "[API] 多檔處理完成："
        f"成功 {success_count}，"
        f"失敗 {failed_count}",
        flush=True,
    )

    # 207 代表部分成功、部分失敗。
    if failed_count > 0:
        return JSONResponse(
            status_code=207,
            content=response,
        )

    return response