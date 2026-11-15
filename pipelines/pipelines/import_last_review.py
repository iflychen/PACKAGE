"""Create a review task from a saved Aniki result without rerunning OCR."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from app import create_review_task, init_review_db
from cmm_review_adapter import make_cmm_review_payload, make_generic_review_payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result_json", help="all_pages_result.json from a previous OCR run")
    parser.add_argument("--name", default=None, help="Display filename for the review task")
    parser.add_argument("--type", choices=("cmm", "generic"), default=None)
    args = parser.parse_args()

    result_path = Path(args.result_json).expanduser().resolve()
    result = json.loads(result_path.read_text(encoding="utf-8-sig"))
    document_type = args.type or (
        "cmm" if result.get("processing_mode") == "original_cmm" else "generic"
    )
    file_name = args.name or result_path.name
    adapter = make_cmm_review_payload if document_type == "cmm" else make_generic_review_payload
    payload = adapter(result, file_name)
    init_review_db()
    token = create_review_task(
        file_name=payload["file_name"],
        metadata=payload["metadata"],
        columns=payload["columns"],
        rows=payload["rows"],
    )
    print(f"人工修正表格：http://localhost:8000/review/{token}")
    print(f"文件類型：{document_type}")
    print(f"辨識列數：{len(payload['rows'])}")


if __name__ == "__main__":
    main()
