import json
import os
import re
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Tuple
from urllib.parse import urlparse

import psycopg
from dotenv import load_dotenv
from psycopg import sql


# =========================
# 1. 基本設定
# =========================

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

# override=True：修改 .env 後，以 .env 的新值覆蓋舊環境變數
load_dotenv(ENV_PATH, override=True)

DB_SCHEMA = "public"

TABLE_SOURCE_FILE = "來源檔案"
TABLE_PART = "品號"
TABLE_PROCESS = "製程"
TABLE_MACHINE = "機台"
TABLE_JOB = "工件"
TABLE_BALL_SIZE = "球標尺寸"
TABLE_MEASUREMENT = "測量值"


# =========================
# 2. Neon 連線
# =========================

def get_database_url() -> str:
    database_url = os.getenv("DATABASE_URL", "").strip()

    if not database_url:
        raise RuntimeError(
            f"找不到 DATABASE_URL，請檢查：{ENV_PATH}"
        )

    return database_url


def get_database_host() -> str:
    parsed = urlparse(get_database_url())
    return parsed.hostname or "unknown"


def test_neon_connection() -> Tuple[str, str, str]:
    """
    回傳：
    - database name
    - database user
    - database host
    """

    with psycopg.connect(
        get_database_url(),
        connect_timeout=15,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT current_database(), current_user;"
            )
            result = cursor.fetchone()

    if result is None:
        raise RuntimeError("Neon 沒有回傳連線測試結果")

    return str(result[0]), str(result[1]), get_database_host()


# =========================
# 3. 資料轉換
# =========================

def normalize_text(value: Any) -> Optional[str]:
    if value is None:
        return None

    text = str(value).strip()

    if text.lower() in {
        "",
        "null",
        "none",
        "unclear",
        "nan",
    }:
        return None

    return text


def to_decimal(value: Any) -> Optional[Decimal]:
    text = normalize_text(value)

    if text is None:
        return None

    text = text.replace(",", "")

    try:
        return Decimal(text)
    except InvalidOperation as error:
        raise ValueError(
            f"無法轉換成數字：{value}"
        ) from error


def parse_datetime_text(value: Any) -> Optional[datetime]:
    """
    解析 build_file_info() 產生的：
    2026-07-13 16:20:30
    """

    text = normalize_text(value)

    if text is None:
        return None

    accepted_formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
    ]

    for date_format in accepted_formats:
        try:
            return datetime.strptime(text, date_format)
        except ValueError:
            pass

    raise ValueError(f"無法解析日期時間：{text}")


def parse_measurement_time(
    metadata: Mapping[str, Any],
) -> Optional[datetime]:
    """
    將 PDF metadata：
      日期：2026年6月09日
      時間：08时36分09秒

    轉成 TIMESTAMP（不含時區）：
      2026-06-09 08:36:09
    """

    date_text = normalize_text(metadata.get("日期"))
    time_text = normalize_text(
        metadata.get("時間") or metadata.get("时间")
    )

    if not date_text:
        return None

    date_match = re.search(
        r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})",
        date_text,
    )

    if not date_match:
        raise ValueError(f"無法解析量測日期：{date_text}")

    year, month, day = map(int, date_match.groups())

    hour = 0
    minute = 0
    second = 0

    if time_text:
        time_match = re.search(
            r"(\d{1,2})\D+(\d{1,2})\D+(\d{1,2})",
            time_text,
        )

        if not time_match:
            raise ValueError(f"無法解析量測時間：{time_text}")

        hour, minute, second = map(int, time_match.groups())

    return datetime(
        year,
        month,
        day,
        hour,
        minute,
        second,
    )


def judge_abnormal(
    deviation: Optional[Decimal],
    upper_tolerance: Optional[Decimal],
    lower_tolerance: Optional[Decimal],
) -> Tuple[bool, Optional[str]]:
    if deviation is None:
        return False, None

    if (
        upper_tolerance is not None
        and deviation > upper_tolerance
    ):
        return True, "超過上公差"

    if (
        lower_tolerance is not None
        and deviation < lower_tolerance
    ):
        return True, "低於下公差"

    return False, None


def iter_rows(
    final_output: Mapping[str, Any],
) -> Iterable[Mapping[str, Any]]:
    pages = final_output.get("pages") or []

    for page in pages:
        if not isinstance(page, Mapping):
            continue

        rows = page.get("rows") or []

        for row in rows:
            if isinstance(row, Mapping):
                yield row


# =========================
# 4. SQL 工具
# =========================

def table_identifier(table_name: str) -> sql.Composed:
    return sql.SQL("{}.{}").format(
        sql.Identifier(DB_SCHEMA),
        sql.Identifier(table_name),
    )


def validate_required_columns(
    cursor: psycopg.Cursor,
) -> None:
    required_tables = {
        TABLE_SOURCE_FILE: {
            "檔名",
            "路徑",
            "上傳時間",
            "檔案內容",
        },
        TABLE_PART: {
            "品號",
        },
        TABLE_PROCESS: {
            "品號",
            "製程",
        },
        TABLE_MACHINE: {
            "機台",
        },
        TABLE_JOB: {
            "品號",
            "製程",
            "機台",
            "流水號",
            "量測時間",
            "量測人員",
        },
        TABLE_BALL_SIZE: {
            "品號",
            "製程",
            "球標尺寸名",
            "上公差",
            "下公差",
            "定義值",
        },
        TABLE_MEASUREMENT: {
            "品號",
            "製程",
            "機台",
            "流水號",
            "球標尺寸名",
            "實際值",
            "是否異常",
            "異常類型",
        },
    }

    for table_name, required_columns in required_tables.items():
        cursor.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = %s
              AND table_name = %s;
            """,
            (DB_SCHEMA, table_name),
        )

        actual_columns = {
            str(row[0])
            for row in cursor.fetchall()
        }

        if not actual_columns:
            raise RuntimeError(
                f"找不到資料表：{DB_SCHEMA}.{table_name}"
            )

        missing_columns = required_columns - actual_columns

        if missing_columns:
            missing_text = "、".join(sorted(missing_columns))
            raise RuntimeError(
                f"資料表「{table_name}」缺少欄位：{missing_text}"
            )


# =========================
# 5. Upsert
# =========================

def upsert_source_file(
    cursor: psycopg.Cursor,
    file_info: Mapping[str, Any],
) -> None:
    file_name = normalize_text(file_info.get("檔名"))

    if not file_name:
        raise ValueError("file_info 裡沒有「檔名」")

    upload_time = parse_datetime_text(
        file_info.get("上傳時間")
    )

    update_query = sql.SQL(
        """
        UPDATE {}
        SET
            "路徑" = %s,
            "上傳時間" = %s,
            "檔案內容" = %s
        WHERE "檔名" = %s;
        """
    ).format(table_identifier(TABLE_SOURCE_FILE))

    cursor.execute(
        update_query,
        (
            normalize_text(file_info.get("路徑")),
            upload_time,
            normalize_text(file_info.get("檔案內容")),
            file_name,
        ),
    )

    if cursor.rowcount == 0:
        insert_query = sql.SQL(
            """
            INSERT INTO {} (
                "檔名",
                "路徑",
                "上傳時間",
                "檔案內容"
            )
            VALUES (%s, %s, %s, %s);
            """
        ).format(table_identifier(TABLE_SOURCE_FILE))

        cursor.execute(
            insert_query,
            (
                file_name,
                normalize_text(file_info.get("路徑")),
                upload_time,
                normalize_text(file_info.get("檔案內容")),
            ),
        )

def upsert_part(
    cursor: psycopg.Cursor,
    part_number: str,
) -> None:
    query = sql.SQL(
        """
        INSERT INTO {} ("品號")
        SELECT %s
        WHERE NOT EXISTS (
            SELECT 1
            FROM {}
            WHERE "品號" = %s
        );
        """
    ).format(
        table_identifier(TABLE_PART),
        table_identifier(TABLE_PART),
    )

    cursor.execute(
        query,
        (part_number, part_number),
    )

def upsert_process(
    cursor: psycopg.Cursor,
    part_number: str,
    process_name: str,
) -> None:
    query = sql.SQL(
        """
        INSERT INTO {} (
            "品號",
            "製程"
        )
        SELECT %s, %s
        WHERE NOT EXISTS (
            SELECT 1
            FROM {}
            WHERE "品號" = %s
              AND "製程" = %s
        );
        """
    ).format(
        table_identifier(TABLE_PROCESS),
        table_identifier(TABLE_PROCESS),
    )

    cursor.execute(
        query,
        (
            part_number,
            process_name,
            part_number,
            process_name,
        ),
    )

def upsert_machine(
    cursor: psycopg.Cursor,
    machine: str,
) -> None:
    query = sql.SQL(
        """
        INSERT INTO {} ("機台")
        SELECT %s
        WHERE NOT EXISTS (
            SELECT 1
            FROM {}
            WHERE "機台" = %s
        );
        """
    ).format(
        table_identifier(TABLE_MACHINE),
        table_identifier(TABLE_MACHINE),
    )

    cursor.execute(
        query,
        (machine, machine),
    )

def upsert_job(
    cursor: psycopg.Cursor,
    part_number: str,
    process_name: str,
    machine: str,
    serial_number: str,
    measurement_time: Optional[datetime],
    operator: Optional[str],
) -> None:
    update_query = sql.SQL(
        """
        UPDATE {}
        SET
            "量測時間" = %s,
            "量測人員" = %s
        WHERE "品號" = %s
          AND "製程" = %s
          AND "機台" = %s
          AND "流水號" = %s;
        """
    ).format(table_identifier(TABLE_JOB))

    cursor.execute(
        update_query,
        (
            measurement_time,
            operator,
            part_number,
            process_name,
            machine,
            serial_number,
        ),
    )

    if cursor.rowcount == 0:
        insert_query = sql.SQL(
            """
            INSERT INTO {} (
                "品號",
                "製程",
                "機台",
                "流水號",
                "量測時間",
                "量測人員"
            )
            VALUES (%s, %s, %s, %s, %s, %s);
            """
        ).format(table_identifier(TABLE_JOB))

        cursor.execute(
            insert_query,
            (
                part_number,
                process_name,
                machine,
                serial_number,
                measurement_time,
                operator,
            ),
        )

def upsert_ball_size(
    cursor: psycopg.Cursor,
    part_number: str,
    process_name: str,
    item_name: str,
    upper_tolerance: Optional[Decimal],
    lower_tolerance: Optional[Decimal],
    nominal_value: Optional[Decimal],
) -> None:
    update_query = sql.SQL(
        """
        UPDATE {}
        SET
            "上公差" = %s,
            "下公差" = %s,
            "定義值" = %s
        WHERE "品號" = %s
          AND "製程" = %s
          AND "球標尺寸名" = %s;
        """
    ).format(table_identifier(TABLE_BALL_SIZE))

    cursor.execute(
        update_query,
        (
            upper_tolerance,
            lower_tolerance,
            nominal_value,
            part_number,
            process_name,
            item_name,
        ),
    )

    if cursor.rowcount == 0:
        insert_query = sql.SQL(
            """
            INSERT INTO {} (
                "品號",
                "製程",
                "球標尺寸名",
                "上公差",
                "下公差",
                "定義值"
            )
            VALUES (%s, %s, %s, %s, %s, %s);
            """
        ).format(table_identifier(TABLE_BALL_SIZE))

        cursor.execute(
            insert_query,
            (
                part_number,
                process_name,
                item_name,
                upper_tolerance,
                lower_tolerance,
                nominal_value,
            ),
        )

def upsert_measurement(
    cursor: psycopg.Cursor,
    part_number: str,
    process_name: str,
    machine: str,
    serial_number: str,
    item_name: str,
    actual_value: Decimal,
    is_abnormal: bool,
    abnormal_type: Optional[str],
) -> None:
    update_query = sql.SQL(
        """
        UPDATE {}
        SET
            "實際值" = %s,
            "是否異常" = %s,
            "異常類型" = %s
        WHERE "品號" = %s
          AND "製程" = %s
          AND "機台" = %s
          AND "流水號" = %s
          AND "球標尺寸名" = %s;
        """
    ).format(table_identifier(TABLE_MEASUREMENT))

    cursor.execute(
        update_query,
        (
            actual_value,
            is_abnormal,
            abnormal_type,
            part_number,
            process_name,
            machine,
            serial_number,
            item_name,
        ),
    )

    if cursor.rowcount == 0:
        insert_query = sql.SQL(
            """
            INSERT INTO {} (
                "品號",
                "製程",
                "機台",
                "流水號",
                "球標尺寸名",
                "實際值",
                "是否異常",
                "異常類型"
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
            """
        ).format(table_identifier(TABLE_MEASUREMENT))

        cursor.execute(
            insert_query,
            (
                part_number,
                process_name,
                machine,
                serial_number,
                item_name,
                actual_value,
                is_abnormal,
                abnormal_type,
            ),
        )

def save_aniki_result(
    final_output: Mapping[str, Any],
) -> dict[str, int]:
    metadata = final_output.get("metadata") or {}
    file_info = final_output.get("file_info") or {}

    if not isinstance(metadata, Mapping):
        raise ValueError(
            "final_output['metadata'] 格式錯誤"
        )

    if not isinstance(file_info, Mapping):
        raise ValueError(
            "final_output['file_info'] 格式錯誤"
        )

    part_number = normalize_text(metadata.get("品號"))
    process_name = normalize_text(metadata.get("製程"))
    machine = normalize_text(metadata.get("機台"))
    serial_number = normalize_text(metadata.get("流水號"))
    operator = normalize_text(metadata.get("操作者"))

    if not part_number:
        raise ValueError("metadata 裡沒有「品號」")

    if not process_name:
        raise ValueError("metadata 裡沒有「製程」")

    if not machine:
        raise ValueError("metadata 裡沒有「機台」")

    if not serial_number:
        raise ValueError("metadata 裡沒有「流水號」")

    measurement_time = parse_measurement_time(metadata)
    rows = list(iter_rows(final_output))

    counts = {
        "來源檔案": 0,
        "品號": 0,
        "製程": 0,
        "機台": 0,
        "工件": 0,
        "球標尺寸": 0,
        "測量值": 0,
    }

    with psycopg.connect(
        get_database_url(),
        connect_timeout=15,
    ) as connection:
        with connection.cursor() as cursor:
            validate_required_columns(cursor)

            # 先寫父資料表，再寫有外鍵的子資料表
            upsert_source_file(cursor, file_info)
            counts["來源檔案"] = 1

            upsert_part(cursor, part_number)
            counts["品號"] = 1

            upsert_process(
                cursor,
                part_number,
                process_name,
            )
            counts["製程"] = 1

            upsert_machine(cursor, machine)
            counts["機台"] = 1

            upsert_job(
                cursor=cursor,
                part_number=part_number,
                process_name=process_name,
                machine=machine,
                serial_number=serial_number,
                measurement_time=measurement_time,
                operator=operator,
            )
            counts["工件"] = 1

            for row in rows:
                item_name = normalize_text(row.get("項目"))

                if not item_name:
                    continue

                actual_value = to_decimal(row.get("实际值"))
                nominal_value = to_decimal(row.get("名义值"))
                upper_tolerance = to_decimal(row.get("上公差"))
                lower_tolerance = to_decimal(row.get("下公差"))
                deviation = to_decimal(row.get("偏差"))

                if actual_value is None:
                    raise ValueError(
                        f"項目「{item_name}」缺少實際值"
                    )

                is_abnormal, abnormal_type = judge_abnormal(
                    deviation=deviation,
                    upper_tolerance=upper_tolerance,
                    lower_tolerance=lower_tolerance,
                )

                upsert_ball_size(
                    cursor=cursor,
                    part_number=part_number,
                    process_name=process_name,
                    item_name=item_name,
                    upper_tolerance=upper_tolerance,
                    lower_tolerance=lower_tolerance,
                    nominal_value=nominal_value,
                )
                counts["球標尺寸"] += 1

                upsert_measurement(
                    cursor=cursor,
                    part_number=part_number,
                    process_name=process_name,
                    machine=machine,
                    serial_number=serial_number,
                    item_name=item_name,
                    actual_value=actual_value,
                    is_abnormal=is_abnormal,
                    abnormal_type=abnormal_type,
                )
                counts["測量值"] += 1

        connection.commit()

    return counts


# =========================
# 7. 單獨測試
# =========================

def main() -> None:
    database_name, user_name, host_name = test_neon_connection()

    print("Neon 連線成功")
    print(f"主機：{host_name}")
    print(f"資料庫：{database_name}")
    print(f"使用者：{user_name}")

    if len(sys.argv) < 2:
        return

    json_path = Path(sys.argv[1]).resolve()

    if not json_path.is_file():
        raise FileNotFoundError(
            f"找不到 JSON：{json_path}"
        )

    with json_path.open(
        "r",
        encoding="utf-8",
    ) as file:
        final_output = json.load(file)

    counts = save_aniki_result(final_output)

    print("\nNeon 寫入成功")
    print(
        json.dumps(
            counts,
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    try:
        main()

    except Exception as error:
        print("\nNeon 操作失敗")
        print(type(error).__name__)
        print(error)
        raise