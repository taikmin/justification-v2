# PC 이관 교훈 (Lessons Learned)

2026-07-31, 이 프로젝트를 새 PC로 이관하며 겪은 문제와 해결책 기록.
다음에 또 PC를 옮길 때 이 문서를 먼저 읽을 것.

---

## 1. 빠른 이관 체크리스트

```powershell
# 1) Python 설치 (없으면)
winget install --id Python.Python.3.12 -e --accept-package-agreements --accept-source-agreements

# 2) venv 생성 + 의존성
& "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 3) claude CLI 확인 (없으면 npm i -g @anthropic-ai/claude-code)
where claude

# 4) 서버 실행 (--reload 쓰지 말 것, 아래 4번 참고)
.\.venv\Scripts\python.exe -m uvicorn server:app --host 127.0.0.1 --port 8000

# 5) 검증 — 서버가 뜬 것만으로는 부족. 반드시 생성까지 돌려볼 것
curl.exe -X POST http://127.0.0.1:8000/api/generate -H "Content-Type: application/json" -d "{\"itemName\":\"노트북\",\"vendor\":\"테스트상사\"}"
```

**gitignore된 것들은 git에 없으니 따로 옮겨야 함**: `item_selection_examples.json`
(few-shot 예제 데이터), `.env`, `examples_extracted/`.
이게 없어도 서버는 뜨지만 예제 0개로 품질이 떨어진 채 동작한다 —
**조용히 degrade되므로 `/api/example-count`로 반드시 확인할 것.**

---

## 2. 가장 크게 데인 것: Windows `claude.CMD` + 여러 줄 프롬프트

**증상**: `/api/generate`가 500. 오류는 `Expecting value: line 1 column 1 (char 0)`.
Claude가 "헤더는 있는데 내용이 없다"는 대화형 응답을 보냈다.

**원인**: Windows에서 `claude`는 `claude.CMD` 배치 래퍼다.
`subprocess.run([CLAUDE_BIN, "-p", prompt])`로 **여러 줄 문자열을 argv로 넘기면
cmd.exe가 첫 줄에서 잘라먹는다.** 프롬프트 대부분이 유실된다.

**해결**: 프롬프트를 argv가 아니라 **stdin으로** 전달.

```python
subprocess.run([CLAUDE_BIN, "-p"], input=prompt, capture_output=True,
               text=True, encoding="utf-8", timeout=180)
```

**교훈**:
- 한 줄 프롬프트로 한 스모크 테스트는 이 버그를 못 잡는다.
  실제로 처음에 `{"ok": 1}` 테스트가 통과해서 "CLI는 정상"이라고 잘못 결론냈다.
  **검증은 반드시 실제 워크로드로 할 것.**
- 외부 CLI에 긴 텍스트를 넘길 땐 플랫폼 무관하게 stdin이 안전하다.
  (Windows argv 길이 제한 ~8191자 문제도 같이 회피된다)

---

## 3. `shutil.which()`로 실행 파일 경로를 해석할 것

**증상**: `[WinError 2] 지정된 파일을 찾을 수 없습니다`

**원인**: `subprocess.run(["claude", ...])`은 `shell=True` 없이는 `.CMD`/`.BAT`를
PATHEXT로 해석하지 못한다. Linux/macOS에서는 되던 코드가 Windows에서만 깨진다.

**해결**:
```python
CLAUDE_BIN = shutil.which("claude")  # → C:\...\npm\claude.CMD
```

`shell=True`는 쓰지 말 것 — 프롬프트에 셸 메타문자가 있으면 터진다.

---

## 4. 한글 경로에서 uvicorn `--reload`가 고장난다

경로에 `내가 만든 작업들` 같은 한글이 들어가면 WatchFiles가
**변경은 감지하고도 워커 재기동에 실패**한다. 로그에는
`WatchFiles detected changes... Reloading...`만 찍히고 그 뒤
`Started server process`가 안 나온다.

결과: 코드를 고쳐도 **옛 버전이 계속 돌아서** 같은 오류가 반복된다.
이것 때문에 이미 고친 버그를 못 고쳤다고 착각하며 한참 헤맸다.

**대응**:
- `--reload` 없이 실행하고, 코드 수정 후엔 프로세스를 직접 재시작한다.
- 수정했는데 증상이 똑같으면 **먼저 "새 코드가 실제로 로드됐는지" 의심할 것.**
  오류 메시지 문구를 일부러 바꿔보면 즉시 확인된다.
- 근본 해결을 원하면 프로젝트를 ASCII 경로로 옮기는 것도 방법.

---

## 5. 디버깅을 위한 오류 메시지 설계

원래 코드는 이랬다:

```python
s, e = text.find("{"), text.rfind("}") + 1
return json.loads(text[s:e])   # 실패하면 "Expecting value: line 1 column 1"
```

LLM이 JSON 대신 무슨 말을 했는지 **알 수가 없어서** 원인 파악이 불가능했다.
`parse_json_response()`로 분리하고 실패 시 응답 원문을 오류에 포함시키자
원인이 한 번에 드러났다.

**교훈: LLM 응답을 파싱할 땐 실패 시 원문을 반드시 남길 것.**
LLM 출력은 비결정적이라, 로그 없이는 재현조차 어렵다.

---

## 6. 타임아웃은 넉넉하게

`timeout=60`은 `claude -p` 호출에 빠듯하다. 60초를 넘겨 실패했다.
`180`으로 늘렸다. 2번 버그를 고쳤어도 이것 때문에 곧바로 또 실패했을 것이다.

**교훈: 버그를 하나 고쳤다고 끝이 아니다. 이관 시엔 여러 문제가 겹쳐 있다.**
한 번에 하나씩 고치고 매번 실제로 돌려서 확인할 것.

---

## 7. 새 PC에서 놓치기 쉬운 것들

**git / GitHub 도구 — 첫 커밋 시점에 터진다**

```powershell
git config --global user.name "taikmin"
git config --global user.email "lee.taikmin@gmail.com"
winget install --id GitHub.cli -e --accept-package-agreements --accept-source-agreements
```

- **git identity가 없으면 `git commit`이 실패한다.**
  `Author identity unknown ... unable to auto-detect email address`.
  커밋할 게 다 준비된 시점에야 터지므로 미리 설정해 둘 것.
- **`gh` CLI가 없다.** 저장소 공개 여부 확인, PR 생성 등이 막힌다.
  급하면 `https://api.github.com/repos/<owner>/<repo>`로 우회 가능하지만
  (`visibility` 필드), 설치해 두는 편이 낫다.

**그 외**

- **`.gitignore`에 `.venv/`가 없었다.** venv를 만들면 git에 잡히니 추가할 것. (수정 완료)
- `claude` CLI는 로그인 상태가 PC마다 다르다. `claude` 한 번 실행해 인증 확인.
- MCP 커넥터(claude.ai Gmail/Google Drive 등)는 새 PC에서 재인증이 필요하다.
- **이 저장소는 public이다.** `구매 견적 예제/`에는 실제 기관·업체명과 거래정보가
  담긴 견적서 PDF가 있다. gitignore로 막아뒀지만, `git add .`은 쓰지 말고
  파일을 명시적으로 지정할 것. 한 번 푸시하면 히스토리에 남아 되돌리기 어렵다.

---

## 8. 일반화된 원칙

1. **"서버가 떴다" ≠ "동작한다".** 헬스체크 말고 핵심 기능을 end-to-end로 돌려볼 것.
2. **스모크 테스트는 실제 데이터 모양으로.** 단순화한 입력은 실제 버그를 숨긴다.
3. **외부 프로세스 호출은 OS 의존성 1순위 의심 대상.** 경로 해석, 인자 전달, 인코딩.
4. **증상이 안 변하면 코드가 안 바뀐 것을 먼저 의심.**
5. **gitignore된 런타임 데이터를 목록으로 관리할 것.** 없어도 조용히 degrade되는 게 최악.
