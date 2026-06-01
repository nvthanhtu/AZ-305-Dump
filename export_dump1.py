import argparse
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List
import xml.etree.ElementTree as ET


NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}


@dataclass
class ParagraphItem:
    text: str
    images: List[str] = field(default_factory=list)


def parse_relationships(xml_bytes: bytes) -> Dict[str, str]:
    root = ET.fromstring(xml_bytes)
    rels: Dict[str, str] = {}

    for rel in root.findall("rel:Relationship", NS):
        rel_id = rel.attrib.get("Id", "")
        target = rel.attrib.get("Target", "")
        if rel_id and target:
            rels[rel_id] = target

    return rels


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def parse_docx_items(docx_path: Path) -> List[ParagraphItem]:
    items: List[ParagraphItem] = []

    with zipfile.ZipFile(docx_path, "r") as zf:
        doc_xml = zf.read("word/document.xml")
        rel_xml = zf.read("word/_rels/document.xml.rels")

    rel_map = parse_relationships(rel_xml)
    root = ET.fromstring(doc_xml)

    body = root.find("w:body", NS)
    if body is None:
        return items

    for p in body.findall("w:p", NS):
        text_parts = [t.text or "" for t in p.findall(".//w:t", NS)]
        text = normalize_text(" ".join(text_parts))

        images: List[str] = []
        for blip in p.findall(".//a:blip", NS):
            rel_id = blip.attrib.get(f"{{{NS['r']}}}embed", "")
            if not rel_id:
                continue

            target = rel_map.get(rel_id, "")
            if not target:
                continue

            image_name = Path(target).name
            if image_name:
                images.append(image_name)

        if text or images:
            items.append(ParagraphItem(text=text, images=images))

    return items


def extract_images(docx_path: Path, output_images_dir: Path) -> None:
    output_images_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(docx_path, "r") as zf:
        for name in zf.namelist():
            if not name.startswith("word/media/"):
                continue
            target = output_images_dir / Path(name).name
            with zf.open(name) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)


def is_question_start(line: str) -> bool:
    return bool(re.match(r"^Question\s*#\s*\d+", line, flags=re.IGNORECASE))


def find_question_prompt_index(section: List[ParagraphItem]) -> int:
    for idx, item in enumerate(section):
        if "?" in item.text:
            return idx
    return -1


def find_answer_index(section: List[ParagraphItem]) -> int:
    for idx, item in enumerate(section):
        if re.match(r"^Correct\s+Answer\s*:", item.text, flags=re.IGNORECASE):
            return idx
    return -1


def is_noise_line(text: str) -> bool:
    if not text:
        return False
    noise_patterns = [
        r"^Reference\s*:",
        r"^Community vote distribution",
        r"^[A-F]{1,2}\s*\(\d+%\)",
        r"^https?://",
    ]
    return any(re.match(pattern, text, flags=re.IGNORECASE) for pattern in noise_patterns)


def split_option_choices(line: str) -> List[str]:
    matches = list(re.finditer(r"(?<!\w)([A-H])\.\s+", line))
    if not matches:
        return [line]

    # Split when choices are packed into one line (A. ... B. ...), or when line starts with A./B./...
    if matches[0].start() == 0 or len(matches) >= 2:
        parts: List[str] = []

        if matches[0].start() > 0:
            prefix = line[: matches[0].start()].strip()
            if prefix:
                parts.append(prefix)

        starts = [m.start() for m in matches] + [len(line)]
        for i in range(len(starts) - 1):
            segment = line[starts[i] : starts[i + 1]].strip()
            if segment:
                parts.append(f"- {segment}")

        return parts

    return [line]


def format_text_line(text: str) -> List[str]:
    line = text.strip()

    # Convert the check-mark bullet used in dump files to markdown bullet syntax.
    if re.match(r"^(✑|âœ‘)\s*", line):
        line = re.sub(r"^(✑|âœ‘)\s*", "", line)
        return [f"- {line}"]

    # Convert explanation section labels such as "Box 1:" to markdown bullets.
    if re.match(r"^Box\s+\d+\s*:\s*", line, flags=re.IGNORECASE):
        return [f"- {line}"]

    return split_option_choices(line)


def render_items(items: List[ParagraphItem], image_folder_name: str) -> str:
    lines: List[str] = []
    for item in items:
        if item.text:
            lines.extend(format_text_line(item.text))
        for img in item.images:
            lines.append(f"![{img}]({image_folder_name}/{img})")
    return "\n".join(lines).strip()


def split_question_sections(items: List[ParagraphItem]) -> List[List[ParagraphItem]]:
    starts: List[int] = []
    for i, item in enumerate(items):
        if is_question_start(item.text):
            starts.append(i)

    if not starts:
        return []

    sections: List[List[ParagraphItem]] = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(items)
        sections.append(items[start:end])

    return sections


def format_section(section: List[ParagraphItem], image_folder_name: str) -> str:
    answer_idx = find_answer_index(section)
    prompt_idx = find_question_prompt_index(section)

    header_offset = 1 if section and is_question_start(section[0].text) else 0

    if prompt_idx == -1:
        prompt_idx = header_offset

    contexte_items = section[header_offset:prompt_idx]

    q_end = answer_idx if answer_idx != -1 else len(section)
    question_items = section[prompt_idx:q_end]

    answer_items: List[ParagraphItem] = []
    explanation_items: List[ParagraphItem] = []

    if answer_idx != -1:
        answer_items = [section[answer_idx]]
        explanation_items = [
            item
            for item in section[answer_idx + 1 :]
            if not is_noise_line(item.text)
        ]

    contexte_text = render_items(contexte_items, image_folder_name)
    question_text = render_items(question_items, image_folder_name)
    answer_text = render_items(answer_items, image_folder_name)
    explanation_text = render_items(explanation_items, image_folder_name)

    section_blocks = [
        "**Context:**",
        contexte_text if contexte_text else "N/A",
        "",
        "**Question:**",
        question_text if question_text else "N/A",
        "",
        "**Answer:**",
        answer_text if answer_text else "N/A",
        "",
        "**Explication:**",
        explanation_text if explanation_text else "N/A",
    ]

    return "\n".join(section_blocks).strip()


def export_dump(docx_path: Path, output_file: Path, image_dir: Path) -> None:
    items = parse_docx_items(docx_path)
    sections = split_question_sections(items)

    extract_images(docx_path, image_dir)
    image_folder_name = image_dir.name

    output_parts: List[str] = []
    for idx, section in enumerate(sections, start=1):
        title = f"## Question Set {idx}"
        output_parts.append(title)
        output_parts.append("")
        output_parts.append(format_section(section, image_folder_name))
        output_parts.append("")

    content = "\n".join(output_parts).strip() + "\n"
    output_file.write_text(content, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export DumpDOCX into Contexte/Question/Answer/Explication format."
    )
    parser.add_argument(
        "--input",
        default="Dump.docx",
        help="Path to the input DOCX file.",
    )
    parser.add_argument(
        "--output",
        default="dump-export.md",
        help="Path to the output markdown file.",
    )
    parser.add_argument(
        "--images-dir",
        default="dump-images",
        help="Directory where images extracted from DOCX will be stored.",
    )

    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)
    images_path = Path(args.images_dir)

    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    export_dump(input_path, output_path, images_path)
    print(f"Export completed: {output_path}")
    print(f"Images extracted to: {images_path}")


if __name__ == "__main__":
    main()
