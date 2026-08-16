import fitz
import os
import sys

if len(sys.argv) < 2:
    print('使用方式：python check_pdf_direction.py "檢表3.pdf" "檢表4.pdf" "檢表5.pdf"')
    raise SystemExit(1)

for pdf_path in sys.argv[1:]:
    if not os.path.exists(pdf_path):
        print(f"\n找不到檔案：{pdf_path}")
        continue

    doc = fitz.open(pdf_path)
    page = doc[0]

    print("\n" + "=" * 60)
    print(f"檔案：{pdf_path}")
    print(f"頁數：{doc.page_count}")
    print(f"PDF rotation：{page.rotation}")
    print(f"page.rect：{page.rect.width} × {page.rect.height}")
    print(f"mediabox：{page.mediabox}")
    print(f"cropbox：{page.cropbox}")

    stem = os.path.splitext(os.path.basename(pdf_path))[0]

    angles = {
        "原始": 0,
        "右轉90": 90,
        "左轉90": -90,
        "轉180": 180,
    }

    for name, angle in angles.items():
        matrix = fitz.Matrix(1.5, 1.5).prerotate(angle)
        pix = page.get_pixmap(matrix=matrix, alpha=False)

        output_name = f"{stem}_{name}.png"
        pix.save(output_name)

        print(f"已輸出：{output_name}")

    doc.close()