import json
import re
import struct
import zlib
from pathlib import Path


ROOT = Path(__file__).resolve().parent
EXTRACTED = ROOT.parent / "justification for item selection" / "examples_extracted"
OUT = ROOT / "item_selection_examples.json"


ENDOFCHAIN = 0xFFFFFFFE
FREESECT = 0xFFFFFFFF
FATSECT = 0xFFFFFFFD
DIFSECT = 0xFFFFFFFC


class CompoundFile:
    def __init__(self, path):
        self.path = Path(path)
        self.data = self.path.read_bytes()
        if self.data[:8] != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
            raise ValueError("not an OLE compound file")
        self.sector_shift = struct.unpack_from("<H", self.data, 30)[0]
        self.mini_sector_shift = struct.unpack_from("<H", self.data, 32)[0]
        self.sector_size = 1 << self.sector_shift
        self.mini_sector_size = 1 << self.mini_sector_shift
        self.num_fat_sectors = struct.unpack_from("<I", self.data, 44)[0]
        self.first_dir_sector = struct.unpack_from("<I", self.data, 48)[0]
        self.mini_stream_cutoff = struct.unpack_from("<I", self.data, 56)[0]
        self.first_mini_fat_sector = struct.unpack_from("<I", self.data, 60)[0]
        self.num_mini_fat_sectors = struct.unpack_from("<I", self.data, 64)[0]
        self.first_difat_sector = struct.unpack_from("<I", self.data, 68)[0]
        self.num_difat_sectors = struct.unpack_from("<I", self.data, 72)[0]
        self.fat = self._load_fat()
        self.entries = self._load_directory()
        self.root = self.entries[0]
        self.mini_fat = self._load_mini_fat()
        self.mini_stream = self._read_regular_stream(self.root["start"], self.root["size"]) if self.root["size"] else b""

    def _sector_offset(self, sector):
        return 512 + sector * self.sector_size

    def _sector(self, sector):
        off = self._sector_offset(sector)
        return self.data[off : off + self.sector_size]

    def _chain(self, start, fat=None):
        if start in (FREESECT, ENDOFCHAIN):
            return []
        fat = fat or self.fat
        out = []
        seen = set()
        cur = start
        while cur not in (ENDOFCHAIN, FREESECT) and cur < len(fat) and cur not in seen:
            seen.add(cur)
            out.append(cur)
            cur = fat[cur]
        return out

    def _load_fat(self):
        difat = list(struct.unpack_from("<109I", self.data, 76))
        difat = [x for x in difat if x not in (FREESECT, ENDOFCHAIN)]
        cur = self.first_difat_sector
        for _ in range(self.num_difat_sectors):
            sec = self._sector(cur)
            vals = list(struct.unpack("<" + "I" * (self.sector_size // 4), sec))
            difat.extend(x for x in vals[:-1] if x not in (FREESECT, ENDOFCHAIN))
            cur = vals[-1]
            if cur == ENDOFCHAIN:
                break
        fat = []
        for sector in difat[: self.num_fat_sectors]:
            sec = self._sector(sector)
            fat.extend(struct.unpack("<" + "I" * (self.sector_size // 4), sec))
        return fat

    def _read_regular_stream(self, start, size=None):
        chunks = [self._sector(s) for s in self._chain(start)]
        data = b"".join(chunks)
        return data if size is None else data[:size]

    def _load_directory(self):
        raw = self._read_regular_stream(self.first_dir_sector)
        entries = []
        for off in range(0, len(raw), 128):
            ent = raw[off : off + 128]
            if len(ent) < 128:
                continue
            name_len = struct.unpack_from("<H", ent, 64)[0]
            name = ent[: max(0, name_len - 2)].decode("utf-16le", errors="ignore")
            obj_type = ent[66]
            start = struct.unpack_from("<I", ent, 116)[0]
            size = struct.unpack_from("<Q", ent, 120)[0]
            if name:
                entries.append({"name": name, "type": obj_type, "start": start, "size": size})
        return entries

    def _load_mini_fat(self):
        if self.num_mini_fat_sectors == 0:
            return []
        raw = self._read_regular_stream(self.first_mini_fat_sector, self.num_mini_fat_sectors * self.sector_size)
        return list(struct.unpack("<" + "I" * (len(raw) // 4), raw))

    def read_stream(self, name):
        entry = next((e for e in self.entries if e["name"] == name), None)
        if entry is None:
            raise KeyError(name)
        if entry["size"] < self.mini_stream_cutoff and self.mini_fat:
            chunks = []
            for mini_sector in self._chain(entry["start"], self.mini_fat):
                off = mini_sector * self.mini_sector_size
                chunks.append(self.mini_stream[off : off + self.mini_sector_size])
            return b"".join(chunks)[: entry["size"]]
        return self._read_regular_stream(entry["start"], entry["size"])

    def stream_names(self):
        return [e["name"] for e in self.entries if e["type"] == 2]


def clean_text(s):
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]+", " ", s)
    s = re.sub(r"[ \t]+", " ", s)
    lines = [line.strip() for line in s.splitlines()]
    return "\n".join(line for line in lines if line)


def hwp_text(path):
    cf = CompoundFile(path)
    header = cf.read_stream("FileHeader")
    compressed = bool(struct.unpack_from("<I", header, 36)[0] & 1)
    texts = []
    for name in sorted(cf.stream_names()):
        if not name.startswith("Section"):
            continue
        raw = cf.read_stream(name)
        if compressed:
            raw = zlib.decompress(raw, -15)
        pos = 0
        while pos + 4 <= len(raw):
            rec = struct.unpack_from("<I", raw, pos)[0]
            pos += 4
            tag = rec & 0x3FF
            size = (rec >> 20) & 0xFFF
            if size == 0xFFF:
                if pos + 4 > len(raw):
                    break
                size = struct.unpack_from("<I", raw, pos)[0]
                pos += 4
            payload = raw[pos : pos + size]
            pos += size
            if tag == 67:
                texts.append(payload.decode("utf-16le", errors="ignore"))
                texts.append("\n")
    return clean_text("".join(texts))


def target_files():
    return sorted(
        [
            p
            for p in EXTRACTED.rglob("*")
            if p.is_file()
            and ("물품선정" in p.name and "추천" in p.name and "사유" in p.name)
            and p.suffix.lower() == ".hwp"
        ],
        key=lambda p: str(p),
    )


def compact_lines(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    merged = []
    for line in lines:
        if merged and len(line) <= 6 and not re.search(r"[.!?。]$", merged[-1]):
            merged[-1] += " " + line
        else:
            merged.append(line)
    return merged


def value_after_label(text, labels, stop_labels):
    label_pat = "|".join(re.escape(x) for x in labels)
    stop_pat = "|".join(re.escape(x) for x in stop_labels)
    numbered = r"(?:\d+\s*[.)]\s*)?"
    pattern = re.compile(
        rf"(?:^|\n)\s*{numbered}(?:{label_pat})(?:\s*\([^)]*\))?\s*[:：]?\s*(.*?)(?=\n\s*{numbered}(?:{stop_pat})(?:\s*\([^)]*\))?\s*[:：]?|\Z)",
        re.S,
    )
    match = pattern.search(text)
    if not match:
        return ""
    value = match.group(1)
    value = re.sub(r"\n{2,}", "\n", value)
    value = re.sub(r"^[ \t:：]+", "", value.strip())
    return clean_field_value(value)


def clean_field_value(value):
    value = value.strip()
    value = re.sub(r"^\)+\s*", "", value)
    value = re.sub(r"^[-,;:：\s]+", "", value)
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if lines and re.search(r"(업체명|전화|담당자|E-?mail|연락처)", lines[0], re.I):
        lines = lines[1:]
    value = "\n".join(lines)
    value = re.split(r"\n?\s*위와\s+같이\s+당\s+부서에서", value, maxsplit=1)[0]
    value = re.split(r"\n?\s*위와\s+같이\s+사유서를", value, maxsplit=1)[0]
    value = re.sub(r"\n{2,}", "\n", value)
    return value.strip()


def parse_record(path, text):
    labels = [
        "품명",
        "규격",
        "수량",
        "단위",
        "물품선정사유",
        "물품 선정 사유",
        "물품선정및추천사유",
        "업체추천사유",
        "업체 추천 사유",
        "추천업체",
        "계약상대자",
        "업체명",
    ]
    item_name = value_after_label(text, ["품명"], labels[1:])
    selection_reason = value_after_label(text, ["물품선정사유", "물품 선정 사유", "물품선정및추천사유"], labels)
    vendor_reason = value_after_label(text, ["업체추천사유", "업체 추천 사유"], labels)

    # HWP table cells often arrive one per line. Fall back to line adjacency when
    # the regex cannot see enough structure.
    lines = compact_lines(text)
    for i, line in enumerate(lines):
        normalized = re.sub(r"\s+", "", line)
        if not item_name and normalized == "품명" and i + 1 < len(lines):
            item_name = clean_field_value(lines[i + 1])
        if not selection_reason and normalized in ("물품선정사유", "물품선정및추천사유") and i + 1 < len(lines):
            selection_reason = clean_field_value(lines[i + 1])
        if not vendor_reason and normalized == "업체추천사유" and i + 1 < len(lines):
            vendor_reason = clean_field_value(lines[i + 1])

    return {
        "id": path.parent.name,
        "source_file": str(path.relative_to(ROOT)),
        "품명": item_name.strip(),
        "물품선정사유": selection_reason.strip(),
        "업체추천사유": vendor_reason.strip(),
    }


def main():
    records = []
    skipped = []
    errors = []
    for path in target_files():
        try:
            text = hwp_text(path)
            record = parse_record(path, text)
            if record["품명"] or record["물품선정사유"] or record["업체추천사유"]:
                records.append(record)
            else:
                skipped.append({"source_file": str(path.relative_to(ROOT)), "reason": "no target fields found"})
        except Exception as exc:
            errors.append({"source_file": str(path.relative_to(ROOT)), "error": repr(exc)})
    payload = {"records": records, "skipped": skipped, "errors": errors}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"records={len(records)} errors={len(errors)} out={OUT}")
    missing = [
        (r["id"], not r["품명"], not r["물품선정사유"], not r["업체추천사유"])
        for r in records
        if not r["품명"] or not r["물품선정사유"] or not r["업체추천사유"]
    ]
    print("missing_fields=", missing)


if __name__ == "__main__":
    main()
