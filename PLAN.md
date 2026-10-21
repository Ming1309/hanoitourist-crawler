# Tổ chức lại thư mục — dễ tìm đường đọc, không đổi hành vi crawler

## 1. AGREED SCOPE và acceptance criteria

**Nguồn xác nhận:** INTERVIEW của flow `1c28aa71-e400-4733-85ab-3a17ce7245f2`, handoff `/Users/ming/.pi/agent/flow-handoffs/1c28aa71-e400-4733-85ab-3a17ce7245f2/4b3434ca-6f71-4d6a-bf30-0585a28305e8.md`. User đồng ý bốn đề xuất bằng **“đồng ý”**, rồi xác nhận AGREED SCOPE cuối bằng **“ok”**.

**Mục tiêu:** mở source ra biết nhóm nào làm gì và bắt đầu đọc từ đâu, thay vì phải nhớ nhiệm vụ của 24 file Python cùng cấp. Đây là thay đổi cách điều hướng code, không phải viết lại kiến trúc runtime.

**Trong phạm vi:** nhóm source vào `workflows/`, `acquisition/`, `processing/`, `persistence/`, `reporting/`; giữ entry points và định nghĩa dùng chung ở package root; nhóm tests tương ứng, giữ shared conftest/fixtures; sửa import, monkeypatch paths, packaging và SQL resource lookup; thêm bản đồ/thứ tự đọc vào README và sửa hướng dẫn hiện hành bị ảnh hưởng.

**Giữ nguyên:** console CLI `hanoitourist ...`, `python -m hanoitourist_crawler`, discovery/capture/parse/validation/completion/storage/export, retry/recovery/scheduler, accepted-snapshot/identity/transaction rules, cấu hình, schema SQL, versions/dependencies và output semantics. Cho phép đổi internal import paths; **không có old-path shim/re-export facade**.

**Ngoài phạm vi:** tối ưu DB/Excel, deduplicate raw/coverage, sửa logic hoặc error taxonomy, thay interface functions/classes, migration/backfill, đổi dependency/version/config behavior, tách toàn bộ docs, di chuyển exports, live crawl, business DB, scheduler activation, Docker crawler build/deploy. Không mang quyền live của task cũ sang flow này.

| ID | Acceptance criteria đã được user xác nhận |
| --- | --- |
| AC1 | Source/tests được nhóm rõ, README có bản đồ và điểm bắt đầu/thứ tự đọc chính. |
| AC2 | New import paths hoạt động; old flat source modules không còn, không shim; tất cả callers và string monkeypatch paths được sửa đồng bộ. |
| AC3 | Normal installed package chạy được console và module CLI từ ngoài repo; SQL resources có trong wheel/bản cài đặt và schema initialization hoạt động. |
| AC4 | Full offline suite chạy đạt với scratch PostgreSQL riêng và Chromium/native-loopback thật; required integration không skip. |
| AC5 | Bảo toàn toàn bộ công việc trước flow, kể cả modified/untracked files; attribution dùng baseline của flow, không dùng toàn bộ HEAD diff làm task diff. |

## 2. Hiện trạng đã xác minh

### 2.1 Baseline và việc thay PLAN cũ

- Official baseline: `/Users/ming/.pi/agent/flow-baselines/bec0fc45-be5f-4884-a010-ec339136e616`; HEAD `cf915e3e3b8df8483d6b7a72a57a9accd691bce8`, captured `2026-10-20T15:48:10.346Z`, warnings empty.
- Trước khi viết plan này: current tracked/staged patches khớp byte-for-byte với `tracked.patch`/`staged.patch`; `git status --porcelain=v1 -z --untracked-files=all` khớp `status.z`; cả **29 baseline untracked files** và private snapshots khớp hashes trong `untracked.json`. Staged patch đang rỗng.
- Đã đọc **toàn bộ PLAN.md trước khi thay**. Đó là kế hoạch attempt-completion của flow cũ, SHA-256 `8d38ed0c19a1f285830cede88a6dfc7693a276787dff54779f76d0bf3e556155`, không phải scope của task này. Nội dung cũ còn được tái dựng từ official baseline. `attempts.py`, stopped helper, tests và GLOSSARY hiện có là công việc **trước flow**, không triển khai lại.
- ARCHITECT chỉ thay `PLAN.md`; source/tests/config/README chưa được sửa. Không có sub-agent được dùng.

### 2.2 Paths, symbols và dependencies có thật

| Existing path/symbol | Quan sát và ảnh hưởng khi move |
| --- | --- |
| `src/hanoitourist_crawler/cli.py:arguments/main`, `__main__.py` | CLI import workflow/export/Storage; retry/report có lazy imports trong `main`. Entry point trong pyproject là `hanoitourist_crawler.cli:main`; giữ nguyên. |
| `incremental.py:selection_reason/crawl_domestic/recover_batches/_export` | Orchestration daily/recovery; `_export` lazy-import Excel. Giữ selection, lock, budget, checkpoints, freeze/export ordering. |
| `attempts.py:complete_daily/complete_retry/complete_retry_stopped/complete_recovery` | Existing completion interface; daily parse/publication commit riêng, retry chung transaction hậu-network, recovery chỉ committed evidence, stopped không parse/publish. Không sửa body ngoài imports. |
| `pipeline.py:parse_run/collect_run/capture_run/crawl/CrawlBusy` | Existing acquisition/replay seam; lazy-import diagnostics trong parse. Root `PARSER_VERSION` phải tiếp tục lấy từ package root. |
| `retry.py:DiagnosticCollector/select_targets/retry_domestic/retry_report`, `scheduler.py:schedule` | Scoped retry có collector subclass, instrumentation và report; scheduler gọi daily workflow. Chỉ sửa import paths, không chạy scheduler. |
| `collector.py:Collector/RequestPacer/CaptureError`, `navigation.py:GuardedNavigation`, `discovery.py` | Collector dùng parser `inventory/merge_inventory/sources`; discovery dùng parser `_locator/evidence`. Giữ existing fixture adapters/guard policy. |
| `parser.py:parse/inventory`, `normalize.py`, `validation.py:validate`, `fingerprint.py:fingerprint` | Parser phụ thuộc selectors; fingerprint dùng parser `_content_signature`. Không tách các helpers sang module mới hay thay extraction/validation. |
| `storage.py:Storage.init_schema/persist_snapshot/apply_attempt/bind_identity/freeze_batch` | Owns SQL/identity/accepted policy. `init_schema` đọc `files('hanoitourist_crawler').joinpath('sql', name)`. Có lazy imports fingerprint và scope cần sửa. Chỉ đổi resource package và imports. |
| `excel.py:export_excel/export_batch_excel`, `export.py:export_run`, `diagnostics.py:phase/classify/write_report` | Reporting dùng model/shared errors; diagnostics còn dùng ở giữa workflow, không phải runtime stage cuối. Không tối ưu hydrate/workbook/coverage trong task này. |
| `tests/conftest.py` | `FIXTURES = Path(__file__).parent / 'fixtures'`; Storage UUID schemas từ `TEST_DATABASE_URL`; Chromium và HTTP loopback fixtures. Giữ fixture logic và đường dẫn fixtures. |
| `tests/test_attempts.py`, `test_pipeline.py`, `test_retry.py` | Import helper `collector` từ `test_incremental`; test_incremental dùng helpers từ `test_discovery/test_collector`. Discovery/navigation cũng dùng test_collector. Các bare imports sẽ gãy nếu chỉ mv files. |
| `tests/test_parser.py`, `test_collector.py`, `test_discovery.py`, `test_incremental.py`, `test_navigation.py` | Có `from conftest import FIXTURES`; cần qualified tests package import khi thêm package markers. |
| `pyproject.toml` | Setuptools `where = ["src"]`; package-data hiện root `sql/*.sql`; pytest testpaths/markers, không lint/typecheck runner riêng. |
| `Dockerfile`, `compose.yaml` | Dockerfile COPY toàn src và install package, CLI không hardcode moved module path. Không cần sửa hoặc build/deploy; Compose/config giữ nguyên. |

**Cross-group dependencies là có thật:** acquisition sử dụng processing helpers; processing parser sử dụng acquisition selectors. Đây không phải layer diagram một chiều. Không nhân dịp di chuyển để đổi seam, inject interfaces giả định hoặc tạo adapters/framework mới. Giữ depth của existing modules; thay đổi này cải thiện locality khi đọc, không tự nhận là deepening/refactor logic.

### 2.3 Checks thực sự đã chạy trong ARCHITECT

- AST parse **24 source + 22 tests/conftest Python files**: đạt.
- Đọc normal installed package qua distribution metadata: **26 Python/SQL file bytes** hiện khớp source. Chưa test wheel layout mới.
- `git diff --check`: sạch trước khi viết PLAN.
- `uv 0.12.1`, build CLI hỗ trợ wheel/out-dir; local Docker image có `postgres:17-alpine`. Đây chỉ là prerequisite discovery, **chưa chứng minh DB/Chromium/build-cache readiness**.
- Không chạy pytest/browser/DB/schema initialization, không build/install, không network/business DB/scheduler. **582 passed** trong README là kết quả lịch sử, không phải verification của việc move sắp tới.

## 3. Thiết kế đích và trade-offs

### 3.1 Source: năm nhóm trách nhiệm

```text
src/hanoitourist_crawler/
├── __init__.py                 # versions, giữ nguyên
├── __main__.py                 # python -m, giữ nguyên
├── cli.py                      # điểm vào, chỉ sửa imports
├── config.py                   # cấu hình dùng chung, giữ nguyên
├── models.py                   # data structures, giữ nguyên
├── errors.py                   # error formats dùng chung, giữ nguyên
├── workflows/
│   ├── __init__.py             # NEW marker, không re-export
│   ├── incremental.py
│   ├── retry.py
│   ├── attempts.py
│   ├── pipeline.py
│   └── scheduler.py
├── acquisition/
│   ├── __init__.py             # NEW marker
│   ├── discovery.py
│   ├── collector.py
│   ├── navigation.py
│   ├── scope.py
│   └── selectors.py
├── processing/
│   ├── __init__.py             # NEW marker
│   ├── parser.py
│   ├── normalize.py
│   ├── validation.py
│   └── fingerprint.py
├── persistence/
│   ├── __init__.py             # NEW marker
│   ├── storage.py
│   └── sql/
│       ├── 001_initial.sql
│       └── 002_incremental.sql
└── reporting/
    ├── __init__.py             # NEW marker
    ├── excel.py
    ├── export.py
    └── diagnostics.py
```

Không đổi tên file/function/class để giảm review noise. Các `__init__.py` mới empty hoặc docstring-only, không eager import/re-export modules khác. Nhờ vậy import một group không tự khởi tạo workflow/connection và không tạo import cycles mới. Không cần marker cho `sql/`: đây là data directory thuộc persistence package.

**Interface/path examples:** `hanoitourist_crawler.processing.parser.parse`, `hanoitourist_crawler.workflows.attempts.complete_retry`, `hanoitourist_crawler.persistence.storage.Storage`. Type signatures, defaults, exceptions, ordering và transaction contracts giữ nguyên. Root CLI/version/model/error imports không đổi.

Trong một group dùng relative sibling imports như repo đang làm; cross-group dùng `..processing`, `..acquisition`, `..reporting`, `..persistence`; shared/root version imports dùng `..models`, `..errors`, `from .. import PARSER_VERSION/COLLECTOR_VERSION`. Riêng root cli dùng `.workflows`, `.reporting`, `.persistence`. Giữ các lazy imports tại vị trí cũ. `attempts` vẫn import sibling `pipeline` như module object để monkeypatch seam hiện có không bị thay thành bound function khác.

```text
CLI → workflows (daily / scoped retry / legacy / recovery)
          ├─ acquisition → raw checkpoints ───────────────┐
          ├─ processing → validated snapshot             │
          └─ completion → accepted publication           │
                        persistence.Storage ←────────────┘
          → frozen batch → reporting (JSON / Excel / diagnostics)
```

Đây là đường đọc/runtime overview, không phải lệnh buộc các group chỉ được gọi từ trái sang phải.

### 3.2 SQL đi cùng persistence package

- Move cả hai SQL files **byte-for-byte** vào `persistence/sql/`.
- `Storage.init_schema`: đổi duy nhất anchor thành `files('hanoitourist_crawler.persistence').joinpath('sql', name)`; giữ transaction, thứ tự 001/002 và cách execute/read_text hiện có.
- `pyproject.toml`: thay package-data entry bằng **quoted** key `"hanoitourist_crawler.persistence" = ["sql/*.sql"]`; không để TOML diễn giải dotted key thành nested table. Giữ package discovery, scripts, versions, requirements, pytest settings.
- Hash bất biến của 001: `46fe4ef63c4bd5455695971c6bab7e8ec07583f8ca99723a8bec758029c3ab72`.
- Hash bất biến của 002: `9aec1839643e07de3d9e17992d9bcb9443d37db144920885a3b7243546be1a5c`.
- Không schema migration/backfill; di chuyển file SQL không có nghĩa đổi SQL. Installed `Storage.init_schema` và CLI `db init` chỉ được chạy trên scratch trong verification.

### 3.3 Tests mirror groups, không viết lại helpers

```text
tests/
├── __init__.py                 # NEW marker
├── conftest.py                 # shared fixtures, chỉ sửa imports
├── fixtures/                   # giữ toàn bộ names/bytes
├── test_cli.py                 # shared/root CLI tests + outside-repo smoke
├── test_config.py              # giữ nguyên
├── test_errors.py              # giữ nguyên
├── test_package_layout.py      # NEW import/resource contract tests
├── workflows/                 # NEW __init__.py
│   └── test_{incremental,retry,attempts,pipeline,scheduler}.py
├── acquisition/               # NEW __init__.py
│   └── test_{discovery,collector,navigation,scope}.py
├── processing/                # NEW __init__.py
│   └── test_{parser,normalize,validation,fingerprint}.py
├── persistence/               # NEW __init__.py
│   └── test_{storage,catalogue_storage}.py
└── reporting/                 # NEW __init__.py
    └── test_{excel,export,diagnostics}.py
```

Regular tests packages tránh phụ thuộc pytest tự thêm từng test directory vào sys.path:

- `from tests.workflows.test_incremental import collector`.
- `from tests.acquisition.test_collector import MemoryStorage, mock_robots, FetchResponse, Request` theo đúng existing symbols mỗi call site đang dùng.
- `from tests.acquisition.test_discovery import html_routes, URL`.
- `from tests.conftest import FIXTURES`; pytest và helper imports resolve cùng qualified conftest, không tạo bản fixture module thứ hai.

Trong từng slice, qualified imports chỉ trỏ tới **vị trí đã move lúc đó** (ví dụ `tests.test_incremental` trước slice workflows, sau đó mới `tests.workflows.test_incremental`). Không trỏ trước tới modules chưa tồn tại, không đổi PYTHONPATH/pytest import mode, không extract helper framework, không xóa/nới assertion hoặc markers để xanh.

### 3.4 Installed package là nguồn chạy tests

Giữ **normal install**, không editable/.pth workaround hay PYTHONPATH tới src. Ignored `build/lib`/egg-info hiện có có thể đưa flat modules cũ trở lại wheel. Vì vậy build từ **clean temporary project copy** chỉ có pyproject/README và current `src/hanoitourist_crawler`, không copy build/egg-info/pycache. Install wheel đó vào `.venv` bằng offline/no-deps/reinstall-package.

Sau mỗi move slice so **exact file set + bytes** giữa source, wheel và installed package; không chỉ check version hay vài imports. Kết quả cuối dự kiến **29 Python + 2 SQL files**, tức 24 Python hiện có + 5 group markers, không có legacy flat modules hoặc root sql files. Không dọn ignored/user files trong repo để che packaging drift. Không sửa build config ngoài package-data cần thiết.

### 3.5 README và domain docs

Thêm gần đầu README một mục “Bản đồ source và thứ tự đọc” gồm cây rút gọn và đường đọc:

1. `cli.py` → `workflows/incremental.py` để hiểu lệnh và một đợt chạy.
2. `acquisition/discovery.py` → `collector.py`/`navigation.py` để hiểu URL → raw.
3. `processing/parser.py` → `validation.py` → `workflows/attempts.py` để hiểu raw → accepted decision.
4. `persistence/storage.py`/`sql/` để hiểu ghi/đọc và policy.
5. `reporting/excel.py`/`export.py`; riêng retry/recovery/scheduler theo workflow tương ứng.

Ghi rõ diagnostics dùng xuyên các bước và các nhóm không phải independent runtime stages. Bổ sung test tree/targeted commands hiện hành cần thiết và một current follow-up verification theo kết quả thật. **Không sửa counts/kết luận/đường dẫn evidence trong các báo cáo lịch sử** hoặc tách README thành docs/. Không có domain change, do đó **không sửa `GLOSSARY.md`, không tạo ADR**.

## 4. Test seams, xác nhận và chiến lược TDD

| Public seam | Bắt được | Không chứng minh | Trạng thái xác nhận |
| --- | --- | --- | --- |
| Import new qualified modules; discovery old flat paths bằng importlib | Layout contract, missing import/lazy dependency khi exercise, absence của shim/legacy modules trong bản cài đặt | Không tự chứng minh logic nghiệp vụ hoặc mọi nhánh lazy import đã chạy | User đã xác nhận AC2; names chi tiết là implementation trong năm group đã chốt. |
| Console/module CLI trong subprocess ngoài repo, installed resource reader và `Storage.init_schema`/existing roundtrip | Console script, cli wiring/imports, wheel SQL inclusion/resource anchor và actual PostgreSQL initialization | Help không chứng minh network/scheduler; resource bytes không thay schema behavior tests | User đã xác nhận AC3; không cần duyệt lại plan. |
| Existing parser/collector/workflow/Storage/export interfaces với fixture adapters, scratch DB và Chromium/native HTTP | Behavior/transaction/baseline/guard/outputs giữ nguyên sau rewiring; cross-test helper resolution | Không live completeness, source freshness, deploy hoặc hard-kill guarantees | Full offline harness được user xác nhận ở AC4. |
| README/path inspection, source-to-baseline mapping và AST normalization | Readability, exact file coverage và việc không lẫn logic/data changes | AST equality không thay runtime integration | AC1/AC5 đã xác nhận. |

Không mở external seam/framework mới. Dùng existing real PostgreSQL tests và fixture collector/browser adapters, không dựng fake transaction implementation.

**TDD theo từng slice:** thêm một focused layout contract trước khi tạo đường dẫn group đó; chạy đỏ vì `ModuleNotFoundError`/old path còn tồn tại, move nhỏ nhất + rewire + clean wheel reinstall, chạy cùng tracer xanh và behavior tests của slice ngay. Đây là **interface-absence red**, không giả vờ tái hiện lỗi dữ liệu. Existing behavior tests là characterization và có thể xanh trước move; ghi nhận đúng như vậy. Không viết toàn bộ tests trước rồi mới move toàn bộ source.

New `tests/test_package_layout.py`: lần lượt thêm `test_processing_layout`, `test_persistence_layout`, `test_reporting_layout`, `test_acquisition_layout`, `test_workflows_layout`; mỗi test exercise imports của group và assert old module specs không tồn tại. Thêm `test_sql_resources_follow_persistence_layout` ở slice persistence để đọc đúng hai SQL resources từ installed package. Không hardcode business counts/giá.

`tests/test_cli.py`: thêm `test_installed_entry_points_outside_repository`, parameterize console/module commands, subprocess cwd=`tmp_path`, help không cần DATABASE_URL; không skip khi console script thiếu. Giữ tất cả existing CLI assertions và dùng exact scratch-only db-init smoke riêng.

Docs/markers/move thuần túy không có behavior bug thích hợp để tạo red. Dùng diff, exact resource/source/wheel bytes, collection coverage và preserved existing tests làm alternative verification. Không manufacture test failure hoặc sửa assertions cũ để có red/green đẹp.

## 5. Các bước IMPLEMENT theo dependency và vertical slices

Mọi bước sau đây là **planned**, chưa chạy ở ARCHITECT. Không stage/commit; filesystem moves phải hỗ trợ cả tracked và untracked baseline files, không dùng `git mv` như cách vô tình stage công việc cũ.

### Bước 1 — Baseline và feedback environment

**What/where:** private evidence directory mới ngoài repo; lưu pre-IMPLEMENT bytes/hashes, current PLAN riêng, baseline manifests, installed file inventory, initial collected test IDs. Reconstruct flow baseline từ HEAD archive + staged.patch + tracked.patch + 29 untracked snapshots khi cần content-level diff. Chuẩn bị clean wheel install, owned scratch PostgreSQL và Chromium preflight; chạy full suite hiện hữu trước move.

**Why/risk:** dirty tree đang chứa nhiều việc trước flow; tests có DDL, không dùng mọi localhost DB như scratch. Build cache/Chromium thiếu hoặc baseline test failure là blocker cần giải thích, không bỏ qua hoặc tự sửa ngoài scope.

**Acceptance:** AC5; có baseline runtime mới và exact inventory để so sau move, không dùng 582 lịch sử làm bằng chứng mới.

### Bước 2 — Raw fixtures → parsed snapshot qua processing paths

**What/where:** tạo tests package markers (root + năm nhóm); sửa bare helper/conftest imports thành qualified **current** paths. Thêm `tests/test_package_layout.py::test_processing_layout`, chạy đỏ trước source move. Move parser/normalize/validation/fingerprint và bốn test files; thêm processing marker. Sửa tất cả current callers gồm collector/discovery/pipeline/Storage/test imports cho bốn modules đã move; không đổi các modules chưa move sang future path.

**Verify:** clean wheel reinstall + file-set/bytes; tracer xanh; processing tests cùng existing `tests/test_pipeline.py` và `tests/test_storage.py`. Kiểm `--collect-only` không mất existing test cases sau package bootstrap.

**Why/risk:** group đầu chứng minh raw → cấu trúc qua interface mới; parser ↔ selectors cross-group cần đúng relative depth. Không đổi inventory/fingerprint/normalization/validation. **AC1/AC2/AC4.**

### Bước 3 — Installed SQL → raw/snapshot roundtrip qua persistence

**What/where:** thêm persistence tracer và SQL resource test, ghi red. Move Storage, hai SQL files, storage/catalogue_storage tests; thêm persistence marker; đổi resource anchor + pyproject package-data như mục 3.2. Sửa root CLI, scheduler, conftest và mọi Storage imports.

**Verify:** clean wheel; SQL names/bytes/hash trong source/wheel/installed; tracer + resource test xanh; `tests/persistence/`, existing `tests/test_pipeline.py`, `tests/test_cli.py`. `test_history_and_roundtrip` đang gọi `init_schema` lại sau fixture initialization nên bảo vệ idempotency, replay versions và raw durability. Giữ identity/publication/frozen policy assertions trong catalogue tests.

**Why/risk:** source đọc SQL được nhưng wheel thiếu SQL là lỗi dễ bị che; installed package + new UUID schemas bắt được. **AC2/AC3/AC4.**

### Bước 4 — Saved data → JSON/XLSX/diagnostics qua reporting

**What/where:** reporting tracer đỏ; move excel/export/diagnostics và ba test files, thêm reporting marker. Rewire root CLI, workflows chưa move và tests, gồm lazy diagnostic/export imports và `from hanoitourist_crawler import excel` → `from hanoitourist_crawler.reporting import excel`.

**Verify:** clean wheel; tracer xanh; reporting tests + `tests/test_cli.py`; preserve atomic bundle/receipt, precision/formula/null/partial-display, diagnostic redaction/private-output checks.

**Why/risk:** string monkeypatch của `os.replace`/`_batch_workbook` và module-object imports phải trỏ cùng implementation, không thêm facade để giữ tests cũ. Không tối ưu Excel. **AC2/AC3/AC4.**

### Bước 5 — Offline browser/listing → durable raw qua acquisition

**What/where:** acquisition tracer đỏ; move năm acquisition modules + bốn test files, thêm marker. Rewire parser selectors, fingerprint/Storage scope consumers, workflows/root imports và test helpers thành `tests.acquisition.*`; giữ nested/lazy imports.

**Verify:** clean wheel; tracer xanh; `tests/acquisition/` + processing tests. Chạy riêng native meta-refresh safety regression rồi cả acquisition group; forbidden targets phải vẫn có zero hits theo assertions hiện hữu.

**Why/risk:** nhiều dependencies đến selectors/collector và DiagnosticCollector; giữ request guard teardown/order, không đổi class defaults hoặc monkeypatch targets để tránh test. **AC2/AC4.**

### Bước 6 — Daily/retry/recovery/legacy orchestration qua workflows và CLI

**What/where:** workflows tracer đỏ; move năm workflow modules và năm test files, thêm marker. Rewire root CLI static/lazy imports, test_incremental helpers sang `tests.workflows.*`, diagnostics/report references. Giữ `attempts` import sibling pipeline module; root versions lấy từ parent package. Thêm outside-repo installed entry-points test vào `tests/test_cli.py`.

**Verify:** clean wheel; tracer xanh; workflows + root CLI + persistence/reporting integration. Completion visibility/native SQL rollback, stopped retry, scoped selection/recovery, no-acquire/no-parse recovery, historical frozen output, scheduler pure/fixture tests phải vẫn chạy. Chỉ chạy scheduler **tests**, không CLI `schedule` ngoài `--help`.

**Why/risk:** daily/retry/recovery deliberately có transaction policies khác nhau; chỉ thay imports, không gộp paths. **AC2/AC3/AC4.**

### Bước 7 — Đường đọc và verification toàn hệ thống

**What/where:** README theo mục 3.5. Final clean wheel/install; full suite với scratch/Chromium; CLI/help/db-init smoke ngoài repo; source/wheel/installed/baseline/AST/collection/dependency/diff checks; attribution report và cleanup owned resources.

**Why/risk:** individual group tests không đủ chứng minh toàn graph/entry points. Final report phải phân biệt tests đã chạy, historical evidence, failures và exceptions; không sửa báo cáo lịch sử. Full harness timeout tối thiểu 1800s mỗi baseline/final invocation, không tăng crawler timeouts để làm tests xanh.

**Acceptance:** AC1–AC5; không remaining required skip/failure/verification exception trừ user explicitly chấp nhận.

## 6. Exact affected-file manifest

Mọi destination dưới đây là **NEW path**; source path cũ bị remove vì move, không để shim. Contents được giữ ngoài import/resource rewiring đã nêu.

### 6.1 Move source và SQL (20 paths)

Prefix source cũ là `src/hanoitourist_crawler/`; destination cùng prefix.

| Old relative path | New relative path |
| --- | --- |
| incremental.py | workflows/incremental.py |
| retry.py | workflows/retry.py |
| attempts.py | workflows/attempts.py |
| pipeline.py | workflows/pipeline.py |
| scheduler.py | workflows/scheduler.py |
| discovery.py | acquisition/discovery.py |
| collector.py | acquisition/collector.py |
| navigation.py | acquisition/navigation.py |
| scope.py | acquisition/scope.py |
| selectors.py | acquisition/selectors.py |
| parser.py | processing/parser.py |
| normalize.py | processing/normalize.py |
| validation.py | processing/validation.py |
| fingerprint.py | processing/fingerprint.py |
| storage.py | persistence/storage.py |
| sql/001_initial.sql | persistence/sql/001_initial.sql |
| sql/002_incremental.sql | persistence/sql/002_incremental.sql |
| excel.py | reporting/excel.py |
| export.py | reporting/export.py |
| diagnostics.py | reporting/diagnostics.py |

### 6.2 Move tests (18 paths)

Prefix cả hai cột là `tests/`.

| Old relative path | New relative path |
| --- | --- |
| test_incremental.py | workflows/test_incremental.py |
| test_retry.py | workflows/test_retry.py |
| test_attempts.py | workflows/test_attempts.py |
| test_pipeline.py | workflows/test_pipeline.py |
| test_scheduler.py | workflows/test_scheduler.py |
| test_discovery.py | acquisition/test_discovery.py |
| test_collector.py | acquisition/test_collector.py |
| test_navigation.py | acquisition/test_navigation.py |
| test_scope.py | acquisition/test_scope.py |
| test_parser.py | processing/test_parser.py |
| test_normalize.py | processing/test_normalize.py |
| test_validation.py | processing/test_validation.py |
| test_fingerprint.py | processing/test_fingerprint.py |
| test_storage.py | persistence/test_storage.py |
| test_catalogue_storage.py | persistence/test_catalogue_storage.py |
| test_excel.py | reporting/test_excel.py |
| test_export.py | reporting/test_export.py |
| test_diagnostics.py | reporting/test_diagnostics.py |

### 6.3 Create (12 files, ngoài destinations của moves)

- `src/hanoitourist_crawler/workflows/__init__.py`
- `src/hanoitourist_crawler/acquisition/__init__.py`
- `src/hanoitourist_crawler/processing/__init__.py`
- `src/hanoitourist_crawler/persistence/__init__.py`
- `src/hanoitourist_crawler/reporting/__init__.py`
- `tests/__init__.py`
- `tests/workflows/__init__.py`
- `tests/acquisition/__init__.py`
- `tests/processing/__init__.py`
- `tests/persistence/__init__.py`
- `tests/reporting/__init__.py`
- `tests/test_package_layout.py`

### 6.4 Edit existing files còn tại chỗ

- `PLAN.md` — ARCHITECT output này, thay historical plan đã đọc.
- `src/hanoitourist_crawler/cli.py` — imports/lazy imports mới, không thay command/options/output/exit codes.
- `tests/conftest.py` — imports mới, giữ mọi fixture bodies/FIXTURES path.
- `tests/test_cli.py` — imports/monkeypatch paths và new outside-repo entry-point test.
- `pyproject.toml` — chỉ package-data owner/path; không đổi requirements/scripts/versions/pytest settings.
- `README.md` — map/read-order/current affected guidance và verification mới theo evidence thật.

### 6.5 Giữ nguyên

Root `__init__.py`, `__main__.py`, `config.py`, `models.py`, `errors.py`; `tests/test_config.py`, `tests/test_errors.py`; mọi `tests/fixtures/`; `GLOSSARY.md`; `.env.example`, `.gitignore`, `.dockerignore`, Dockerfile, Compose; exports/evidence/history; các business DB/containers/volumes/scheduler. Không tạo docs/ hoặc ADR. Không manual edit generated egg-info/build metadata. Không file deletion khác ngoài old locations của moves.

## 7. Exact verification commands và expected outcomes

**Đây là commands cho IMPLEMENT, chưa được chạy trong ARCHITECT.** Chạy từ repo root trừ subshell outside repo. Dùng logs/JUnit/private scripts dưới D, không logs/credential files trong repo; redact secrets. Không echo env/DSN, bật shell tracing hoặc dùng `tee` làm mất exit code.

### 7.1 Environment và clean wheel installation (lặp sau mỗi slice)

```bash
export REPO="$PWD"
export PY="$REPO/.venv/bin/python"
export CLI="$REPO/.venv/bin/hanoitourist"
export PYTHONDONTWRITEBYTECODE=1
unset PYTHONPATH
umask 077
export D="$(mktemp -d "${TMPDIR:-/tmp}/hanoitourist-layout-check.XXXXXX")"
```

Định nghĩa helper tạm trong shell của implement, không thêm build script vào repo:

```bash
install_clean_wheel() {
  export BUILD_DIR="$(mktemp -d "$D/build.XXXXXX")"
  "$PY" -B - <<'PY'
import os, shutil
from pathlib import Path
repo = Path(os.environ['REPO'])
project = Path(os.environ['BUILD_DIR']) / 'project'
project.mkdir()
for name in ('pyproject.toml', 'README.md'):
    shutil.copy2(repo / name, project / name)
shutil.copytree(repo / 'src/hanoitourist_crawler', project / 'src/hanoitourist_crawler',
                ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo'))
PY
  uv build --offline --python "$PY" --wheel \
    --out-dir "$BUILD_DIR/dist" "$BUILD_DIR/project" || return
  export WHEEL="$("$PY" -B -c 'import os; from pathlib import Path; xs=list((Path(os.environ["BUILD_DIR"])/"dist").glob("*.whl")); assert len(xs)==1; print(xs[0])')"
  uv pip install --python "$PY" --offline --no-deps \
    --reinstall-package hanoitourist-crawler "$WHEEL"
}
install_clean_wheel
uv pip check --python "$PY"
```

Không thêm dependency, không `--no-build-isolation` workaround. Nếu declared build dependencies không có offline cache, báo prerequisite; không cài/chạy package cũ thay source mới. Sau **mỗi** install chạy private equality checker: source `*.py/*.sql` relative file set và bytes phải bằng wheel package entries và installed package files (bỏ pycache); installed root không được resolve về repo/src. Check trước baseline và sau từng green slice. Final count 29 Python + 2 SQL; old flat 18 modules và root SQL files không có trong cả source/wheel/installed.

### 7.2 Owned scratch PostgreSQL, không dùng business DB

```bash
docker image inspect postgres:17-alpine --format '{{.Id}}'
export FLOW_RUN="$("$PY" -B -c 'import uuid; print(uuid.uuid4().hex[:12])')"
export SCRATCH_NAME="hanoitourist-layout-$FLOW_RUN"
export POSTGRES_USER=postgres POSTGRES_DB=directory_layout_test
export POSTGRES_PASSWORD="$("$PY" -B -c 'import secrets; print(secrets.token_urlsafe(24))')"
export SCRATCH_ID="$(docker run -d --name "$SCRATCH_NAME" \
  --label purpose=directory-layout-tests --label "flow-run=$FLOW_RUN" \
  -e POSTGRES_USER -e POSTGRES_DB -e POSTGRES_PASSWORD \
  -p 127.0.0.1::5432 --tmpfs /var/lib/postgresql/data:rw postgres:17-alpine)"
export SCRATCH_PORT="$(docker port "$SCRATCH_NAME" 5432/tcp | awk -F: '{print $NF}')"
export TEST_DATABASE_URL="$("$PY" -B -c 'import os; from psycopg.conninfo import make_conninfo; print(make_conninfo(host="127.0.0.1", port=os.environ["SCRATCH_PORT"], dbname=os.environ["POSTGRES_DB"], user=os.environ["POSTGRES_USER"], password=os.environ["POSTGRES_PASSWORD"], connect_timeout="5"))')"
```

Private preflight (ghi script/log trong D khi implement): bounded readiness <= khoảng 35s, verify Docker exact ID/name/two labels/loopback binding và `current_database() == 'directory_layout_test'` trước DDL; không in Docker Config.Env/DSN/password. `TEST_DATABASE_URL` phải là URL vừa tạo, không nhận inherited URL/.env fallback. Các pytest fixtures tạo/drop UUID schemas trong DB này. Chromium launch/close preflight thật; missing Chromium là blocker, không tự download/install/change versions để né lỗi. Không Compose operations hoặc inspect/use credentials từ project DB.

### 7.3 Baseline, red/green và targeted behavior commands

Mỗi invocation lưu command, exit status thật, elapsed time, pass/fail/skip/deselected và JUnit ở D. Harness timeout >=1800s cho baseline/full, không che timeout bằng success. `-ra` phải hiện skip reasons. Dùng scratch env ở mọi command cần DB.

```bash
# B0: trước source/test restructure; inventory + full baseline mới (AC4/AC5)
"$PY" -B -m pytest -p no:cacheprovider --collect-only -q
"$PY" -B -m pytest -p no:cacheprovider -q -ra --junitxml="$D/baseline.xml"

# Sau tests package bootstrap: collection vẫn giữ đủ baseline cases.
"$PY" -B -m pytest -p no:cacheprovider --collect-only -q

# S1 processing tracer: chạy cùng command trước move (đỏ) và sau clean reinstall (xanh)
"$PY" -B -m pytest -p no:cacheprovider -q -ra tests/test_package_layout.py::test_processing_layout
"$PY" -B -m pytest -p no:cacheprovider -q -ra tests/processing/ tests/test_pipeline.py tests/test_storage.py

# S2 persistence tracer/resource: thêm ngay trước slice này, red rồi green
"$PY" -B -m pytest -p no:cacheprovider -q -ra tests/test_package_layout.py -k persistence
"$PY" -B -m pytest -p no:cacheprovider -q -ra tests/persistence/ tests/test_pipeline.py tests/test_cli.py

# S3 reporting tracer: red trước move, green sau reinstall
"$PY" -B -m pytest -p no:cacheprovider -q -ra tests/test_package_layout.py::test_reporting_layout
"$PY" -B -m pytest -p no:cacheprovider -q -ra tests/reporting/ tests/test_cli.py

# S4 acquisition tracer + explicit existing safety regression, sau đó cả group
"$PY" -B -m pytest -p no:cacheprovider -q -ra tests/test_package_layout.py::test_acquisition_layout
"$PY" -B -m pytest -p no:cacheprovider -q -ra \
  tests/acquisition/test_navigation.py::test_html_native_navigation_bounds_fail_closed
"$PY" -B -m pytest -p no:cacheprovider -q -ra tests/acquisition/ tests/processing/

# S5 workflows tracer: red rồi green, các interfaces consumers cùng chạy
"$PY" -B -m pytest -p no:cacheprovider -q -ra tests/test_package_layout.py::test_workflows_layout
"$PY" -B -m pytest -p no:cacheprovider -q -ra \
  tests/workflows/ tests/test_cli.py tests/persistence/ tests/reporting/

# Final: full integrated suite, không filter browser/postgres (AC2–AC5)
"$PY" -B -m pytest -p no:cacheprovider -q -ra --junitxml="$D/final.xml"
```

Expected: B0/new full tests thực sự pass; red tracers đúng missing/new layout, không environment/collection fault; green tracers + behavior suites pass. JUnit final zero failures/errors/skips; không hardcode 582 làm acceptance, không mất existing cases sau mapping paths. Nếu case names/parameters cũ biến mất, giải thích và sửa relocation thay vì giảm suite. Không `pytest -m 'not browser and not postgres'` thay full verification. Native regression failures không được nới hit assertions, tăng budgets hoặc đổi acquisition logic ngoài task.

### 7.4 Installed CLI/resources ngoài repository

Tests CLI mới chạy subprocess từ tmp_path cho cả console và module `--help`, database env absent, exit 0 và cùng command/options hiện hữu. Smoke đầy đủ thêm:

```bash
(cd "$D" && "$CLI" --help)
(cd "$D" && "$PY" -B -m hanoitourist_crawler --help)
for command in db crawl crawl-domestic retry-domestic retry-report export-batch schedule parse export; do
  (cd "$D" && "$CLI" "$command" --help) || break
  (cd "$D" && "$PY" -B -m hanoitourist_crawler "$command" --help) || break
done
# CHỈ sau scratch ownership/DB identity verification; env override riêng từng process.
(cd "$D" && DATABASE_URL="$TEST_DATABASE_URL" "$CLI" db init)
(cd "$D" && DATABASE_URL="$TEST_DATABASE_URL" "$PY" -B -m hanoitourist_crawler db init)
```

Runner phải bắt exit của **từng command** trong vòng lặp; `break` không được tính như toàn loop pass. Help không gọi Config/DB; db-init chỉ có DDL additive trên owned scratch. Kiểm 13 bảng expected trong scratch public schema và lần init thứ hai không đổi schema/rows. New UUID-schema Storage roundtrip tests vẫn là evidence actual SQL loading độc lập với public init idempotency. Đọc hai resources bằng importlib.resources từ installed persistence package, so hash/bytes với baseline SQL và wheel. Không chạy các lệnh crawl/retry/schedule có tác động ngoài `--help`.

### 7.5 Static, preservation và collection checks

```bash
uv pip check --python "$PY"
git diff --check
# Expected: không còn old qualified source path trong executable source/tests.
rg -n 'hanoitourist_crawler\.(attempts|collector|diagnostics|discovery|excel|export|fingerprint|incremental|navigation|normalize|parser|pipeline|retry|scheduler|scope|selectors|storage|validation)\b' src tests
# Expected: không bare cross-test/conftest imports.
rg -n '^\s*(from (test_[a-z_]+|conftest) import|import test_[a-z_]+\b)' tests
```

Với no-match searches, exit 1 là expected; matches phải được xem xét, không bọc thành success. New layout tests có thể chứa old paths như **negative assertions**: whitelist đúng literals trong `tests/test_package_layout.py`, không xóa test để search sạch. Relative imports không được bắt hết bởi rg: AST/import tests mới là kiểm chứng chính.

Private checker trong D khi implement:

- Parse mọi current source/test `.py` bằng `ast.parse` (không compileall/pycache repo). Dự kiến final 29 source + 29 test/conftest/marker files nếu đúng manifest.
- Compare logical pre/post file mapping tại mục 6: normalize Import/ImportFrom qualified paths và exact monkeypatch-path literals theo relocation; cho phép duy nhất SQL resource anchor ở `Storage.init_schema`. Remaining production AST phải bằng baseline; import-free moves và SQL phải byte-equal. Không bỏ qua cả function body/class/strings để tạo false equality. New marker/layout tests/CLI additions được review riêng; existing test assertions, decorators/parameters/fixtures không bị thay ngoài path rewiring.
- Root protected files/fixtures/config/versions/exports giữ exact baseline bytes. `.git` index/staged patch không đổi; không stage dirty work.
- Reconstruct official baseline **file contents**, apply move map rồi compare current tree kể cả untracked destinations. Không dùng diff-of-patches làm implementation diff, không để Git HEAD-only view bỏ mất untracked modules. Phân biệt PLAN-only architect change và source/test/docs implement changes.
- Normalize original collection node IDs theo test move map, assert mọi existing case còn được collect; new contract/CLI cases là phần tăng thêm. Chạy final suite trên đúng installed wheel bytes.
- README map đúng current paths, nêu đường đọc và cross-group caveat; historical evidence blocks không thay kết quả. Lưu attribution/verification report sanitized.

## 8. Rủi ro, rollout, migration và rollback

- **Relative/lazy imports:** một cấp `.` sai hoặc eager package re-export gây cycle/wrong module binding. Empty markers + fresh-process imports + existing runtime tests, giữ lazy position và pipeline module object.
- **Cross-test imports/fixtures:** bare imports hoặc tests package collision có thể mất collection/reimport conftest. Qualified tests paths và unchanged fixtures; collection mapping + full run từ installed package, không sys.path hacks.
- **Stale artifacts:** same-version wheel chứa old flat files từ build/lib, hoặc venv chưa reinstall. Clean temp project/wheel + exact file-set/bytes + negative import checks; không sửa ignored/user artifacts hoặc tin version alone.
- **SQL not packaged:** source-tree pass không đủ. Quoted package-data key + installed resource read/init/roundtrip và immutable hashes; không đưa SQL logic changes vào move.
- **Behavior drift/noise:** không reformat/rename functions/merge policies hoặc tối ưu khi đang move. Body-level AST/content comparison và full offline integration bảo vệ baseline.
- **Prerequisites/skip/timeouts:** local image hiện có không đồng nghĩa Docker ready; Chromium/build cache phải verify khi implement. Required failure/skip không tự thành pass; dừng và báo hoặc xin accepted exception, không truy cập business DB để thay scratch.
- **Historical consumers:** internal imports cũ cố ý không tương thích theo scope đã chốt. README công bố new paths; CLI/output vẫn tương thích. Historical diagnostics scripts dưới ignored exports có thể chứa old imports: giữ bằng chứng đó nguyên, không chạy hay rewrite chúng; rerun script lịch sử là task riêng.

**Rollout:** source/tests/docs + normal local wheel install cho verification, không deploy hoặc cập nhật scheduler image. Dockerfile hiện COPY src nên không cần path change; Docker/container readiness không được chứng nhận bởi task này.

**Migration:** không có DB/schema/data migration hoặc backfill. SQL files chỉ move byte-for-byte. Không business data operation; CLI db init chỉ trong scratch.

**Rollback:** dùng pre-IMPLEMENT snapshot và move map để đảo chỉ các thay đổi flow; giữ lại dirty/untracked content gốc theo official baseline. Không `git reset --hard`, `git clean`, revert về HEAD hoặc xóa untracked source cũ của user. Nếu cần phục hồi runtime local, dựng/install lại clean wheel từ preserved pre-move source; không đổi dependencies/schema/version để rollback.

**Cleanup:** trước remove phải verify exact `SCRATCH_ID`, name, labels purpose/flow-run; kiểm không còn UUID test schemas rồi `docker rm -f "$SCRATCH_ID"` chỉ với container do lượt này tạo. Không Compose down, volume prune hoặc xóa container khác. Xóa private DSN/secret file nếu có, unset TEST_DATABASE_URL/POSTGRES_PASSWORD và credential env do lượt này tạo; giữ sanitized reports/logs/red→green evidence. Không overwrite hoặc di chuyển exports/evidence cũ.

## 9. Open questions và required handoff

**Không còn blocking product/design question.** Scope, compatibility/no-shim, tests grouping, docs extent và required verification đã được user xác nhận. Group membership, regular package markers, qualified test imports và resource owner là technical choices trong scope, không cần second approval của toàn plan. Môi trường runtime chưa được chứng nhận; prerequisite failure ở IMPLEMENT phải xử lý như blocker/explicit exception, không âm thầm hạ acceptance.

IMPLEMENT handoff phải có: exact moved/created/edited paths so baseline gồm untracked; red→green import tracers phân biệt characterization; fresh baseline/final commands, durations/counts/skips/JUnit; scratch ownership/cleanup không secrets; source/wheel/installed/resources/AST/collection/help/dependencies/diff evidence; README map và historical-preservation review; mọi failures/accepted exceptions; xác nhận không live/business DB/scheduler/deploy, không domain/glossary/ADR changes. Kết quả offline không chứng nhận live completeness/freshness hoặc production deployment.
