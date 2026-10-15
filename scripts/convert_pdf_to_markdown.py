"""Convert a text-based PDF to page-structured Markdown with rendered page images."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from pypdf import PdfReader


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_pdf", type=Path)
    parser.add_argument("output_markdown", type=Path)
    parser.add_argument("--dpi", type=int, default=150)
    return parser.parse_args()


def clean_text(text: str) -> str:
    text = text.replace("\r", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def main() -> None:
    args = parse_args()
    source = args.input_pdf.resolve()
    output = args.output_markdown.resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if args.dpi < 72:
        raise ValueError("--dpi 必须不小于 72")

    assets_dir = output.parent / f"{output.stem}_assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    # Windows builds of pdftocairo can fail when either path contains Chinese.
    # Render through a temporary ASCII-only path, then copy page images back.
    with tempfile.TemporaryDirectory(prefix="pdf_markdown_") as temp_dir:
        stage_dir = Path(temp_dir)
        staged_source = stage_dir / "source.pdf"
        shutil.copy2(source, staged_source)
        subprocess.run(
            [
                "pdftocairo",
                "-png",
                "-r",
                str(args.dpi),
                str(staged_source),
                str(stage_dir / "page"),
            ],
            check=True,
        )
        for rendered_page in stage_dir.glob("page-*.png"):
            shutil.copy2(rendered_page, assets_dir / rendered_page.name)

    reader = PdfReader(str(source))
    title = (reader.metadata.title if reader.metadata else None) or source.stem
    lines = [
        f"# {source.stem}",
        "",
        f"> 来源 PDF：`{source.name}`",
        f"> 页数：{len(reader.pages)}；转换方式：PDF 文字层提取 + {args.dpi} DPI 页面渲染。",
        "> 图纸、接线图和流程图以页面图片保留；除原文可提取标签外，未自动推断连接关系。",
        "",
        "## 使用说明",
        "",
        "- 可使用下方文本检索规格、参数与 Modbus 命令。",
        "- 涉及接线、端子、电压、电流和安全状态时，请以对应页图及原始 PDF 为准。",
        "- PDF 有双栏排版，文字按页面阅读顺序提取，表格视觉布局可能与原件略有差异。",
        "",
    ]
    for index, page in enumerate(reader.pages, start=1):
        image_name = f"page-{index}.png"
        image_path = assets_dir / image_name
        # pdftocairo names files page-1.png, page-2.png, ...
        if not image_path.exists():
            raise FileNotFoundError(f"未找到渲染页图：{image_path}")
        text = clean_text(page.extract_text() or "")
        relative_image = f"{assets_dir.name}/{image_name}"
        lines.extend([
            f"## 第 {index} 页",
            "",
            f"![第 {index} 页原图]({relative_image})",
            "",
            "### 可检索文字",
            "",
            text or "_此页未提取到文字层，请查看原图。_",
            "",
        ])

    output.write_text("\n".join(lines), encoding="utf-8")
    print(f"Markdown: {output}")
    print(f"Assets: {assets_dir}")


if __name__ == "__main__":
    main()
