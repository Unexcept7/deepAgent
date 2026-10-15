"""Convert PDFs to editable DOCX files using an existing Word package as template.

The PDF text layer becomes editable Word paragraphs. Embedded PDF raster images
are extracted separately and inserted as images; whole-page screenshots are not
used.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
PIC_NS = "http://schemas.openxmlformats.org/drawingml/2006/picture"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"

NS = {"w": W_NS, "r": R_NS}
ET.register_namespace("w", W_NS)
ET.register_namespace("r", R_NS)
ET.register_namespace("wp", WP_NS)
ET.register_namespace("a", A_NS)
ET.register_namespace("pic", PIC_NS)


def run(command: list[str]) -> str:
    result = subprocess.run(command, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return result.stdout


def png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return (1200, 800)
    return (int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big"))


def text_paragraph(parent: ET.Element, text: str, style: str | None = None) -> None:
    p = ET.SubElement(parent, f"{{{W_NS}}}p")
    if style:
        ppr = ET.SubElement(p, f"{{{W_NS}}}pPr")
        ET.SubElement(ppr, f"{{{W_NS}}}pStyle", {f"{{{W_NS}}}val": style})
    run_el = ET.SubElement(p, f"{{{W_NS}}}r")
    t = ET.SubElement(run_el, f"{{{W_NS}}}t")
    if text.startswith(" ") or text.endswith(" "):
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    t.text = text


def image_paragraph(parent: ET.Element, rel_id: str, image_name: str, image_id: int, path: Path) -> None:
    width, height = png_size(path)
    max_width, max_height = 6_200_000, 8_800_000
    scale = min(max_width / (width * 914400 / 180), max_height / (height * 914400 / 180), 1.0)
    cx, cy = int(width * 914400 / 180 * scale), int(height * 914400 / 180 * scale)
    p = ET.SubElement(parent, f"{{{W_NS}}}p")
    r = ET.SubElement(p, f"{{{W_NS}}}r")
    drawing = ET.SubElement(r, f"{{{W_NS}}}drawing")
    inline = ET.SubElement(drawing, f"{{{WP_NS}}}inline", {"distT": "0", "distB": "0", "distL": "0", "distR": "0"})
    ET.SubElement(inline, f"{{{WP_NS}}}extent", {"cx": str(cx), "cy": str(cy)})
    ET.SubElement(inline, f"{{{WP_NS}}}docPr", {"id": str(image_id), "name": image_name})
    graphic = ET.SubElement(inline, f"{{{A_NS}}}graphic")
    graphic_data = ET.SubElement(graphic, f"{{{A_NS}}}graphicData", {"uri": PIC_NS})
    pic = ET.SubElement(graphic_data, f"{{{PIC_NS}}}pic")
    nv = ET.SubElement(pic, f"{{{PIC_NS}}}nvPicPr")
    ET.SubElement(nv, f"{{{PIC_NS}}}cNvPr", {"id": "0", "name": image_name})
    ET.SubElement(nv, f"{{{PIC_NS}}}cNvPicPr")
    fill = ET.SubElement(pic, f"{{{PIC_NS}}}blipFill")
    ET.SubElement(fill, f"{{{A_NS}}}blip", {f"{{{R_NS}}}embed": rel_id})
    stretch = ET.SubElement(fill, f"{{{A_NS}}}stretch")
    ET.SubElement(stretch, f"{{{A_NS}}}fillRect")
    sppr = ET.SubElement(pic, f"{{{PIC_NS}}}spPr")
    xfrm = ET.SubElement(sppr, f"{{{A_NS}}}xfrm")
    ET.SubElement(xfrm, f"{{{A_NS}}}off", {"x": "0", "y": "0"})
    ET.SubElement(xfrm, f"{{{A_NS}}}ext", {"cx": str(cx), "cy": str(cy)})
    geom = ET.SubElement(sppr, f"{{{A_NS}}}prstGeom", {"prst": "rect"})
    ET.SubElement(geom, f"{{{A_NS}}}avLst")


def replace_xml_from_template(template: Path, output: Path, source: Path, pages: list[list[str]], images: list[tuple[Path, int]]) -> None:
    with zipfile.ZipFile(template) as zin:
        files = {name: zin.read(name) for name in zin.namelist() if not name.endswith("/")}

    root = ET.Element(f"{{{W_NS}}}document")
    body = ET.SubElement(root, f"{{{W_NS}}}body")
    text_paragraph(body, source.stem, "Title")
    text_paragraph(body, f"来源 PDF：{source.name}")
    text_paragraph(body, "正文按 PDF 文字层提取；图片按原 PDF 内嵌对象单独保留。")

    for page_no, lines in enumerate(pages, start=1):
        text_paragraph(body, f"第 {page_no} 页", "Heading1")
        nonempty = False
        for line in lines:
            line = line.rstrip()
            if line.strip():
                text_paragraph(body, line)
                nonempty = True
        if not nonempty:
            text_paragraph(body, "[此页没有可提取的文字层，请查看原 PDF 中的图形内容。]")
        if page_no < len(pages):
            p = ET.SubElement(body, f"{{{W_NS}}}p")
            r = ET.SubElement(p, f"{{{W_NS}}}r")
            ET.SubElement(r, f"{{{W_NS}}}br", {f"{{{W_NS}}}type": "page"})

    rel_root = ET.fromstring(files["word/_rels/document.xml.rels"])
    for rel in list(rel_root):
        if rel.attrib.get("Type", "").endswith("/image"):
            rel_root.remove(rel)
    existing_ids = [int(re.sub(r"\D", "", r.attrib.get("Id", "0")) or 0) for r in rel_root]
    next_id = max(existing_ids, default=0) + 1
    media_names = []
    for image, _page_no in images:
        name = f"image_custom_{len(media_names) + 1}.png"
        media_names.append(name)
        files[f"word/media/{name}"] = image.read_bytes()
        ET.SubElement(rel_root, f"{{{REL_NS}}}Relationship", {"Id": f"rId{next_id}", "Type": f"{R_NS}/image", "Target": f"media/{name}"})
        next_id += 1
    files["word/_rels/document.xml.rels"] = ET.tostring(rel_root, encoding="utf-8", xml_declaration=True)

    # Put each extracted image after the text for the page where it occurs.
    # Insert in reverse page order so earlier indexes remain stable.
    page_images: dict[int, list[tuple[int, Path, str]]] = {}
    for image_index, ((image, page_no), name) in enumerate(zip(images, media_names), start=1):
        rel_id = f"rId{max(existing_ids, default=0) + image_index}"
        page_images.setdefault(page_no, []).append((image_index, image, name))
    for page_no in sorted(page_images, reverse=True):
        children = list(body)
        heading = f"第 {page_no} 页"
        heading_index = next(
            (i for i, child in enumerate(children)
             if child.tag == f"{{{W_NS}}}p"
             and "".join(t.text or "" for t in child.findall(f".//{{{W_NS}}}t")) == heading),
            None,
        )
        if heading_index is None:
            continue
        next_heading = next(
            (i for i in range(heading_index + 1, len(children))
             if children[i].tag == f"{{{W_NS}}}p"
             and re.fullmatch(r"第 \d+ 页", "".join(t.text or "" for t in children[i].findall(f".//{{{W_NS}}}t")))),
            len(children),
        )
        insert_at = next_heading
        if insert_at > 0 and children[insert_at - 1].find(f".//{{{W_NS}}}br") is not None:
            insert_at -= 1
        for image_index, image, name in reversed(page_images[page_no]):
            image_paragraph(body, f"rId{max(existing_ids, default=0) + image_index}", name, image_index, image)
            element = body[-1]
            body.remove(element)
            body.insert(insert_at, element)
    sect = ET.SubElement(body, f"{{{W_NS}}}sectPr")
    ET.SubElement(sect, f"{{{W_NS}}}pgSz", {f"{{{W_NS}}}w": "11906", f"{{{W_NS}}}h": "16838"})
    ET.SubElement(sect, f"{{{W_NS}}}pgMar", {f"{{{W_NS}}}{x}": "720" for x in ("top", "right", "bottom", "left")})
    files["word/document.xml"] = ET.tostring(root, encoding="utf-8", xml_declaration=True)

    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zout:
        for name, data in files.items():
            if name.startswith("word/media/") and name not in {f"word/media/{n}" for n in media_names}:
                continue
            zout.writestr(name, data)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = args.pdf.resolve()
    output = args.output.resolve()
    with tempfile.TemporaryDirectory(prefix="pdf_editable_word_") as temp:
        temp_path = Path(temp)
        staged_pdf = temp_path / "source.pdf"
        shutil.copy2(source, staged_pdf)
        pages = []
        for page in range(1, int(run(["pdfinfo", str(staged_pdf)]).split("Pages:")[1].splitlines()[0].strip()) + 1):
            text = run(["pdftotext", "-layout", "-enc", "UTF-8", "-f", str(page), "-l", str(page), str(staged_pdf), "-"])
            pages.append(text.splitlines())
        image_dir = temp_path / "images"
        image_dir.mkdir()
        run(["pdfimages", "-png", str(staged_pdf), str(image_dir / "image")])
        images = sorted(image_dir.glob("*.png"))
        image_pages = []
        for line in run(["pdfimages", "-list", str(staged_pdf)]).splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[0].isdigit() and fields[1].isdigit():
                image_pages.append(int(fields[0]))
        image_items = [(image, image_pages[index] if index < len(image_pages) else len(pages)) for index, image in enumerate(images)]
        replace_xml_from_template(args.template.resolve(), output, source, pages, image_items)
    print(output)


if __name__ == "__main__":
    main()
