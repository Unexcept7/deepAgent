from __future__ import annotations

import argparse
from pathlib import Path

from pypdf import PdfReader, PdfWriter, Transformation
from pypdf._page import PageObject


def make_reordered_pdf(source: Path, target: Path, split_ratio: float = 0.5) -> None:
    reader = PdfReader(str(source), strict=False)
    writer = PdfWriter()

    if not reader.pages:
        raise ValueError("输入 PDF 没有页面")
    if not 0.45 <= split_ratio <= 0.55:
        raise ValueError("split_ratio 应位于 0.45 到 0.55 之间")

    page_map: list[str] = []
    for source_index, source_page in enumerate(reader.pages, start=1):
        width = float(source_page.mediabox.width)
        height = float(source_page.mediabox.height)
        split_x = width * split_ratio
        left_width = split_x
        right_width = width - split_x

        # Each output page keeps the original page's vector/text content. Only
        # the visible half is placed onto the narrower destination page.
        left_page = PageObject.create_blank_page(width=left_width, height=height)
        left_page.merge_transformed_page(
            source_page,
            Transformation(),
            over=True,
            expand=False,
        )
        writer.add_page(left_page)
        page_map.append(f"derived_page={len(writer.pages)}; original_page={source_index}; column=left")

        right_page = PageObject.create_blank_page(width=right_width, height=height)
        right_page.merge_transformed_page(
            source_page,
            Transformation().translate(tx=-split_x, ty=0),
            over=True,
            expand=False,
        )
        writer.add_page(right_page)
        page_map.append(f"derived_page={len(writer.pages)}; original_page={source_index}; column=right")

    writer.add_metadata(
        {
            "/Title": "XP3018 IO模块（8路）- 左栏右栏重排版",
            "/Subject": "每个原始页面按左栏→右栏重排，供 RAGFlow 单栏解析",
            "/SourceDocument": source.name,
            "/ReadingOrder": "original_page_left_then_right",
            "/ColumnSplitRatio": f"{split_ratio:.4f}",
            "/PageMapping": " | ".join(page_map),
        }
    )

    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as stream:
        writer.write(stream)


def main() -> None:
    parser = argparse.ArgumentParser(description="将双栏 PDF 按每页左栏→右栏重排")
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("--split-ratio", type=float, default=0.5)
    args = parser.parse_args()
    make_reordered_pdf(args.source, args.target, args.split_ratio)
    print(f"已生成: {args.target}")


if __name__ == "__main__":
    main()
