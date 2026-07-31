import json
import shutil
import subprocess
from io import BytesIO
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

EXAMPLES_JSON = Path(__file__).parent / "item_selection_examples.json"
app = FastAPI(title="물품선정사유 자동생성")

# Windows에서 claude는 claude.CMD 배치 파일이라 shell 없이는 해석되지 않음
CLAUDE_BIN = shutil.which("claude")


def call_claude(prompt: str) -> str:
    """claude CLI를 subprocess로 호출.

    프롬프트는 argv가 아닌 stdin으로 전달한다. Windows의 claude.CMD 배치
    래퍼는 여러 줄 인자를 첫 줄에서 잘라내므로 argv로 넘기면 프롬프트
    대부분이 유실된다.
    """
    if not CLAUDE_BIN:
        raise RuntimeError("claude CLI를 찾을 수 없음 (PATH 확인 필요)")
    result = subprocess.run(
        [CLAUDE_BIN, "-p"],
        input=prompt,
        capture_output=True, text=True, encoding="utf-8", timeout=180
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "claude CLI 오류")
    return result.stdout.strip()


def parse_json_response(text: str) -> dict:
    """claude 응답에서 JSON 객체를 추출. 실패 시 원문을 오류에 포함."""
    s, e = text.find("{"), text.rfind("}") + 1
    if s == -1 or e <= s:
        raise RuntimeError(f"JSON 응답이 아님: {text[:500]!r}")
    try:
        return json.loads(text[s:e])
    except json.JSONDecodeError as err:
        raise RuntimeError(f"JSON 파싱 실패 ({err}): {text[s:e][:500]!r}") from err


# ── 예제 로딩 ─────────────────────────────────────────────────────────────────

def load_examples() -> list[dict]:
    if not EXAMPLES_JSON.exists():
        return []
    data = json.loads(EXAMPLES_JSON.read_text("utf-8"))
    return [
        r for r in data.get("records", [])
        if r.get("품명") and r.get("물품선정사유") and r.get("업체추천사유")
    ]


def pick_examples(item_name: str, examples: list[dict], n: int = 3) -> list[dict]:
    if not examples:
        return []
    if not item_name:
        return examples[:n]
    tokens = set(item_name.lower().split())
    def score(ex):
        target = (ex["품명"] + " " + ex.get("물품선정사유", "")).lower()
        return sum(1 for t in tokens if t in target)
    return sorted(examples, key=score, reverse=True)[:n]


# ── 파일 텍스트 추출 ──────────────────────────────────────────────────────────

def extract_text_xlsx(data: bytes) -> str:
    import openpyxl
    wb = openpyxl.load_workbook(BytesIO(data), data_only=True)
    rows = []
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            cells = [
                str(c.value).strip()
                for c in row
                if c.value is not None and str(c.value).strip()
            ]
            if cells:
                rows.append("  ".join(cells))
    return "\n".join(rows)


def extract_text_xls(data: bytes) -> str:
    import xlrd
    wb = xlrd.open_workbook(file_contents=data)
    rows = []
    for ws in wb.sheets():
        for r in range(ws.nrows):
            cells = [
                str(ws.cell_value(r, c)).strip()
                for c in range(ws.ncols)
                if ws.cell_value(r, c) and str(ws.cell_value(r, c)).strip() not in ("", "None")
            ]
            if cells:
                rows.append("  ".join(cells))
    return "\n".join(rows)


def extract_text_pdf(data: bytes) -> str:
    import pdfplumber
    with pdfplumber.open(BytesIO(data)) as pdf:
        parts = [page.extract_text() for page in pdf.pages if page.extract_text()]
    return "\n".join(parts)


def extract_raw_text(filename: str, data: bytes) -> str:
    ext = Path(filename).suffix.lower()
    if ext in (".xlsx", ".xlsm"):
        return extract_text_xlsx(data)
    if ext == ".xls":
        try:
            return extract_text_xls(data)
        except Exception:
            return extract_text_xlsx(data)
    if ext == ".pdf":
        return extract_text_pdf(data)
    if ext in (".txt", ".csv"):
        return data.decode("utf-8", errors="replace")
    raise ValueError(f"지원하지 않는 파일 형식: {ext}")


# ── Claude 호출 ───────────────────────────────────────────────────────────────

def claude_extract(raw_text: str) -> dict:
    """견적서 원문 → 주요 필드 추출 (haiku: 빠르고 저렴)"""
    prompt = f"""다음은 한국어 견적서 원문입니다. 아래 JSON 형식으로만 응답하세요.

--- 견적서 ---
{raw_text[:4000]}
--- 끝 ---

{{
  "품명": "실제로 구매하는 물건·서비스·소프트웨어의 정식 명칭",
  "규격": "제품 사양·모델명 (없으면 빈 문자열)",
  "업체명": "공급업체 이름 (없으면 빈 문자열)",
  "담당자": "업체 담당자 이름 (없으면 빈 문자열)",
  "전화번호": "업체 전화번호 (없으면 빈 문자열)",
  "금액": "총 견적금액 (없으면 빈 문자열)",
  "견적일자": "견적서 작성일 (없으면 빈 문자열)"
}}

품명 추출 규칙:
- "품명:", "품목:", "제품명:" 라벨 바로 옆·아래 값이 품명
- 업체명·담당자·주소·날짜·금액은 품명이 아님
- 라벨이 없으면 실제로 돈을 내고 구매하는 물건/서비스 이름
- JSON만 출력, 설명 없이"""

    return parse_json_response(call_claude(prompt))


class GenerateRequest(BaseModel):
    principal: str = ""
    account: str = ""
    projectName: str = ""
    purpose: str = ""
    notes: str = ""
    vendor: str = ""
    vendorContact: str = ""
    vendorPhone: str = ""
    itemName: str = ""
    spec: str = ""
    quantity: str = ""
    unit: str = ""
    amount: str = ""
    quotationDate: str = ""


def claude_generate(req: GenerateRequest, examples: list[dict]) -> dict:
    """입력 정보 + 예제 → 물품선정사유·업체추천사유 생성"""
    ex_block = ""
    if examples:
        snippets = [
            f"품명: {ex['품명']}\n물품선정사유: {ex['물품선정사유']}\n업체추천사유: {ex['업체추천사유']}"
            for ex in examples
        ]
        ex_block = "【실제 작성 예시】\n\n" + "\n\n---\n\n".join(snippets) + "\n\n"

    qty_unit = f"{req.quantity} {req.unit}".strip()
    item_out = f"{req.itemName} ({req.spec})" if req.spec else req.itemName
    vendor_header = f"5. 업체추천사유 (업체명: {req.vendor}, 연락처 및 담당자: {req.vendorPhone}, {req.vendorContact})"

    prompt = f"""{ex_block}【작성할 구매 정보】
과제명: {req.projectName}
연구책임자: {req.principal}
과제계정: {req.account}
품명: {req.itemName}
규격: {req.spec}
수량/단위: {qty_unit}
금액: {req.amount}
견적일자: {req.quotationDate}
업체명: {req.vendor}
업체 담당자: {req.vendorContact} / {req.vendorPhone}
구매 목적: {req.purpose}
추가 사항: {req.notes}

아래 JSON만 출력하세요:
{{
  "품명출력": "{item_out}",
  "물품선정사유": "4. 물품선정사유 (용도개요 포함)\\n[본문]",
  "업체추천사유": "{vendor_header}\\n[본문]"
}}

작성 원칙:
- 한국 공공기관 공문서 문체, ~함/~임/~됨 으로 끝맺음
- 물품선정사유 본문: 이 물품이 과제 수행에 왜 필요한지 3~5문장
- 업체추천사유 본문: 이 업체를 선정한 이유 3~4문장
- 입력된 정보만 사용, 없는 사실 추가 금지
- 헤더 형식은 정확히 지킬 것:
  물품선정사유 → 첫 줄: "4. 물품선정사유 (용도개요 포함)", 다음 줄부터 본문
  업체추천사유 → 첫 줄: "{vendor_header}", 다음 줄부터 본문
- JSON만 출력"""

    return parse_json_response(call_claude(prompt))


# ── 엔드포인트 ─────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "index.html")


@app.get("/api/example-count")
def example_count():
    return {"count": len(load_examples())}


@app.post("/api/extract")
async def extract(file: UploadFile = File(...)):
    data = await file.read()
    try:
        raw_text = extract_raw_text(file.filename or "", data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"파일 파싱 오류: {e}")

    try:
        fields = claude_extract(raw_text)
    except Exception as e:
        raise HTTPException(500, f"Claude 추출 오류: {e}")

    fields["raw_text"] = raw_text[:12000]
    return JSONResponse(fields)


@app.post("/api/generate")
async def generate(req: GenerateRequest):
    examples = load_examples()
    selected = pick_examples(req.itemName, examples)
    try:
        result = claude_generate(req, selected)
    except Exception as e:
        raise HTTPException(500, f"Claude 생성 오류: {e}")
    return JSONResponse(result)
