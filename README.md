# Hanoitourist — domestic incremental crawler

Python CLI khám phá **tour trong nước** tại `https://hanoitourist.vn/tour-trong-nuoc` và pagination đã xác minh, tải chi tiết incremental và xuất XLSX full/delta. Scheduler chạy hằng ngày qua Compose profile riêng. Không crawl tour quốc tế hoặc toàn website.

Lệnh legacy `crawl` vẫn chạy thủ công cho URL:

https://hanoitourist.vn/kham-pha-phu-quoc-hon-ngoc-vien-dong

Thu thập nội dung tour, mọi accordion lịch trình, ảnh dạng URL và tất cả lịch khởi hành/giá chi tiết. Lưu raw staging và dữ liệu quan hệ trong PostgreSQL, giữ lịch sử từng lần chạy, xuất JSON để đối chiếu và Excel để xem trực quan. Có crawl danh sách và scheduler; không có UI.

Discovery hiện chỉ chạy `HTMLDiscoveryCollector` (HTML best-effort, không bảo đảm đủ danh mục). Đã bỏ phương thức crawl listing bằng JavaScript cũ; `DiscoveryCollector` chỉ giữ các phương thức dùng chung cho listing. Các mục nghiệm thu lịch sử bên dưới mô tả trạng thái tại thời điểm kiểm chứng, không phải adapter cũ vẫn được hỗ trợ. Detail collector, scheduler, incremental, schema và dữ liệu đã lưu không đổi.

## Bản đồ source và thứ tự đọc

Source được nhóm theo **trách nhiệm** để dễ tìm đường đọc:

```text
src/hanoitourist_crawler/
├── __init__.py                 # Versions
├── __main__.py                 # python -m hanoitourist_crawler
├── cli.py                      # Điểm vào: lệnh nào gọi luồng nào?
├── config.py                   # Cấu hình dùng chung
├── models.py                   # Run, raw, snapshot và các cấu trúc dữ liệu
├── errors.py                   # Định dạng lỗi dùng chung
├── workflows/                  # Điều phối công việc
│   ├── incremental.py          # Một đợt crawl domestic và recovery
│   ├── retry.py                # Retry có phạm vi và báo cáo retry
│   ├── attempts.py             # Completion và quyết định publication
│   ├── pipeline.py             # Capture, parse/replay và crawl legacy
│   └── scheduler.py            # Điều phối chạy theo lịch
├── acquisition/                # Website → raw checkpoints
│   ├── discovery.py            # Tìm link từ listing
│   ├── collector.py            # Browser và tương tác trang chi tiết
│   ├── navigation.py           # Guard request/redirect
│   ├── scope.py                # URL và phạm vi cho phép
│   └── selectors.py            # Vị trí nội dung trên trang
├── processing/                 # Raw → dữ liệu có cấu trúc
│   ├── parser.py               # Bóc tách nội dung, lịch và giá
│   ├── normalize.py            # Chuẩn hóa giá trị nguồn
│   ├── validation.py           # Kiểm tra chất lượng và coverage
│   └── fingerprint.py          # So sánh nội dung nghiệp vụ
├── persistence/                # PostgreSQL và catalogue policy
│   ├── storage.py              # Lưu/đọc, identity và accepted refs
│   └── sql/                    # 001_initial.sql, 002_incremental.sql
└── reporting/                  # Dữ liệu đã lưu → output/báo cáo
    ├── excel.py                # XLSX từng run và full/delta batch
    ├── export.py               # JSON từng run
    └── diagnostics.py          # Observations và diagnostic reports
```

**Bắt đầu ở `cli.py`, sau đó đọc `workflows/incremental.py`.** Muốn theo một tour từ website đến nơi lưu trữ, đọc theo thứ tự:

1. `acquisition/discovery.py` → `collector.py` và `navigation.py`: tìm URL, tải/tương tác và lưu raw.
2. `processing/parser.py` → `normalize.py` → `validation.py`: đọc raw đã commit, bóc tách và kiểm tra dữ liệu.
3. `workflows/attempts.py` → `persistence/storage.py`: hiểu khác biệt daily/retry/recovery, transaction và việc chấp nhận snapshot. Parser-complete không tự đồng nghĩa accepted.
4. `reporting/excel.py` hoặc `export.py`: hiểu output từ snapshot/frozen refs.

Với luồng riêng: đọc `workflows/retry.py` cho scoped retry, `workflows/incremental.py:recover_batches` cho recovery và `workflows/scheduler.py` cho lịch chạy. `models.py`, `config.py` và `errors.py` là các định nghĩa dùng chung, tra cứu khi cần.

Các thư mục **không phải các stage runtime độc lập hoặc các layer chỉ phụ thuộc một chiều**: acquisition dùng parser helpers để ghi inventory, parser dùng selectors, Storage dùng fingerprint/scope; diagnostics ghi observations cả trong lúc workflow chạy. Mỗi nhóm có `__init__.py` chỉ làm package marker, không facade/re-export.

Tests nằm trong `tests/{workflows,acquisition,processing,persistence,reporting}/` tương ứng; `tests/conftest.py` và `tests/fixtures/` dùng chung. CLI/config/errors và package-layout tests ở root `tests/`. Ví dụ chọn tests theo câu hỏi đang đọc (các ca PostgreSQL cần `TEST_DATABASE_URL` tới **DB thử nghiệm riêng**, browser cần Chromium):

```bash
.venv/bin/python -B -m pytest -p no:cacheprovider -q tests/processing/
.venv/bin/python -B -m pytest -p no:cacheprovider -q tests/workflows/test_attempts.py
.venv/bin/python -B -m pytest -p no:cacheprovider -q tests/test_package_layout.py tests/test_cli.py
```

Các lệnh CLI và output không đổi. **Import nội bộ đã chuyển, không giữ shim ở đường dẫn phẳng cũ**; ví dụ dùng `from hanoitourist_crawler.processing.parser import parse` và `from hanoitourist_crawler.persistence.storage import Storage`. SQL resources thuộc package `hanoitourist_crawler.persistence`, được đóng gói dưới `persistence/sql/`; không thay SQL/schema. `exports/` và các báo cáo kiểm chứng lịch sử giữ nguyên vị trí/nội dung; script lịch sử có import cũ không được tự động cập nhật hay coi là tương thích với layout mới.

## Thư mục kết quả

- `exports/domestic/<batch-id>/`: các bundle Excel danh mục; giữ nguyên đường dẫn và nội dung vì DB lưu receipt của chúng.
- `exports/latest/`: lối tắt tới bundle mới nhất hiện có tại thời điểm dọn thư mục, gồm `full.xlsx` và `delta.xlsx`. Chưa tự cập nhật khi có batch mới; dùng đường dẫn CLI in ra cho những lần chạy tiếp theo.
- `archive/verification/{diagnostics,quality}/`: log, script và tài liệu kiểm chứng cũ đã chuyển khỏi `exports/`.
- `archive/single-tour-exports/<run-id>/`: các bản xuất một tour cũ.

Đợt dọn chỉ chuyển file, không thay DB hoặc hành vi exporter. Đường dẫn ghi trong báo cáo/script kiểm chứng cũ có thể vẫn trỏ về vị trí trước khi chuyển; các script này là tài liệu lịch sử, không chạy lại trực tiếp. Các lệnh crawl/retry tiếp theo vẫn tạo output theo quy tắc bên dưới. Cả `exports/` và `archive/` được Git bỏ qua; archive không phải bản backup DB.

## Cài đặt

Yêu cầu Python >=3.11 (môi trường kiểm chứng dùng 3.13), PostgreSQL >=15, Chromium của Playwright. Docker chỉ cần nếu dùng PostgreSQL local qua Compose.

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python '.[dev]'
.venv/bin/python -m playwright install chromium
cp .env.example .env
```

Nếu không dùng uv: tạo venv bằng `python3 -m venv .venv`, rồi cài bằng `.venv/bin/python -m pip install '.[dev]'`.

**Cài package bình thường, không dùng `-e`**: môi trường macOS này đánh dấu `.pth` hidden, Python bỏ qua editable import. Sau khi sửa source, cài lại trước khi chạy/test:

```bash
uv pip install --python .venv/bin/python --reinstall-package hanoitourist-crawler .
```

**Khi nâng từ layout phẳng cũ:** không đóng wheel bằng cách tái sử dụng `build/lib` cũ, vì setuptools có thể đưa các module đã di chuyển trở lại bản cài đặt. Build từ checkout/bản sao source sạch, không mang theo `build/`, `*.egg-info/` hoặc `__pycache__/`, rồi cài wheel bình thường. Verification của đợt chuyển thư mục dùng bản sao tạm sạch và so exact file sets/bytes; không xóa build artifacts hoặc công việc có sẵn trong repo để né vấn đề này.

## PostgreSQL

Có thể đặt `DATABASE_URL` tới DB đã có quyền tạo bảng. Không dùng DB production để thử nghiệm.

Hoặc dùng Compose local:

```bash
docker compose up -d --wait
.venv/bin/hanoitourist db init
```

Chỉnh `.env` trước khi chạy. Nếu cổng 5432 đã có dịch vụ, đổi **cả hai** `POSTGRES_PORT` và port trong `DATABASE_URL` sang cổng trống, ví dụ 55432. `POSTGRES_*` cấu hình container; `DATABASE_URL` cấu hình CLI. Mật khẩu mẫu chỉ dành cho local. Không commit `.env`.

`db init` idempotent, không DROP hay ghi đè dữ liệu. Migration được đóng gói trong Python package; CLI hoạt động ngoài thư mục repo khi các biến môi trường đã được cấu hình. CLI không tự migrate mỗi lần crawl. Compose dùng persistent volume; không chạy `down -v` nếu muốn giữ staging/lịch sử.

**DB đã tạo trong quá trình kiểm chứng:** project Compose `hanoitourist-crawler-verify`, PostgreSQL tại `localhost:55432`. Để dùng lại dữ liệu kiểm chứng, cấu hình `.env` với port 55432 và credential local trong `.env.example`; quản lý container bằng `docker compose -p hanoitourist-crawler-verify ...`. Không khởi động thêm project Compose ở cùng cổng này. Container dự án khác trên 5432 không bị sửa.

## Incremental tour trong nước

Sau khi nâng package, chạy **`hanoitourist db init`** trên DB đang dùng để áp dụng `002_incremental.sql` additive/idempotent; không tạo DB trắng hoặc recrawl baseline bắt buộc. Không tự sửa/xóa staging và snapshots cũ.

```bash
.venv/bin/hanoitourist db init
.venv/bin/hanoitourist crawl-domestic
.venv/bin/hanoitourist crawl-domestic --headed --output-dir exports
# Offline: dùng refs đã đóng băng của batch, không browser/parse/network:
.venv/bin/hanoitourist export-batch --batch-id <UUID> --output-dir exports
```

- `crawl-domestic` chọn **HTML discovery best-effort** ngay trước acquisition: context Chromium riêng tắt JavaScript, chặn service workers và mọi subresource/iframe/XHR/POST. Chỉ primary HTTP documents của listing/pager trong root domestic được guarded fetch; cards/provenance lấy từ raw HTTP, không từ listing DOM. Không request Modernizr, không fallback sau browser JS error. Detail collector vẫn dùng browser/JS và thu đầy đủ như trước; HTML mode vẫn cần Chromium.
- Discovery **luôn partial, terminal false, coverage chưa đảm bảo**, kể cả thấy pager cuối hoặc explicit empty. Manifest: `discovery_adapter=domestic-html-best-effort-v1`, `coverage_basis=guarded_http_document`, `coverage_guaranteed=false`; `source_pager_terminal_observed` chỉ mô tả source pager, không chứng minh inventory. Giữ duplicate/unresolved descriptors, raw refs và actual stop reason; controls/scope/pager/loops/access/MIME/limits lỗi thì dừng bảo thủ. Failure trước usable scoped observation là failed. Listing source từng không ổn định giữa scans; không union scans/AJAX để giả complete.
- Tour mới lấy đầy đủ bằng collector/parser hiện có. Tour đã biết tận dụng snapshot complete cũ; chỉ recheck sau **7 ngày tính từ `finished_at` của lần capture + parse complete gần nhất**. Replay, export và attempts partial/failed không kéo dài TTL. `RECHECK_DAYS` cấu hình được.
- Best-effort partial discovery **không probe vì vắng listing, không unlist/delete hoặc đổi availability do omission**. Tour observed new/due/retry/reactivated vẫn được chọn; fresh accepted được skip. Tour unseen được retry nếu đã `requires_refresh`; ngoại lệ backlog là tour catalogue đang `pending`, chưa có accepted snapshot và chưa từng attempt vẫn được chọn `new`, kể cả khi lần trước hết budget trước lượt tải. Guard unavailable/superseded/identity/dedup vẫn áp dụng; không phải mọi tour thiếu snapshot đều được ưu tiên. Unseen known due không tự được chọn chỉ vì quá 7 ngày, nên không cam kết recheck đủ catalogue trong 7 ngày. Absence probes chỉ dành cho injected discovery có complete+terminal được xác minh, không phải public HTML adapter. Chỉ HTTP **404/410 cuối của navigation tour chính do guard sở hữu**, có flags `owned_primary=true`, `terminal=true` và raw evidence khớp run/GET-or-HEAD/role/status/URL, mới đánh dấu `unavailable`. Callback diagnostic, AJAX/iframe/listing/resource 404, lỗi tạm thời/parse/access gate không làm vậy. Evidence cũ thiếu flags không được tự nâng trust khi recovery; runs/batches đã frozen không bị backfill. Unavailable được giữ cùng lịch sử; xuất hiện lại trên listing thì tải ngay và chỉ khôi phục sau complete.
- URL aliases được gộp theo source node đã xác minh, không theo tên tour. Sau merge, selection đọc lại active state và bỏ entity đã xử lý/superseded, không fetch lại target từ state stale hoặc ghi đè outcome đã publish thành skipped. URL mới chưa biết identity vẫn cần detail verification; không đoán dedup theo title. Publication current snapshot + watermark + fingerprint + change chỉ chấp nhận complete. Failed/partial giữ complete baseline; tour chưa từng complete vẫn có trạng thái `pending` và dữ liệu attempt được ghi rõ là incomplete.
- Fingerprint `v1` bao phủ nội dung nghiệp vụ (kể cả link/ảnh/giá/ô gộp), không UUID/parser/capture time/provenance hay UI state đã whitelist. Cùng URL ảnh nhưng alt/title khác nhau được giữ thành các observations riêng; provenance trỏ về element nguồn thật, kể cả data-src/srcset. Mỗi lần recheck thành công vẫn giữ run/raw/snapshot lịch sử dù `unchanged`.
- Mọi lệnh network crawl dùng cùng **session advisory lock PostgreSQL** trong schema đang dùng. Busy không tạo batch/run mới (CLI exit 2); parse/export offline vẫn chạy được. Không giữ SQL transaction xuyên suốt browser.
- Batch/attempt commits và refs frozen cho phép phục hồi khi crash; batch dang dở được finalize/export trước batch mới, không xóa lịch sử hay chạy lại cùng scheduled slot. Evidence của capture cũ không ghi đè trạng thái đã publish từ capture mới hơn; lượt cũ được ghi skipped và giữ nguyên run/raw/snapshot. Daily crawl bị ngắt terminalize các attempt đang pending của chính invocation và đặt requires_refresh trước freeze, không parse/publish capture chưa hoàn tất. Retry lỗi export dùng `export-batch`, không recrawl. API nội bộ `recover_batches(..., batch_id=...)` giới hạn recovery đúng một batch; caller phải giữ crawl lock và xác minh batch không còn owner. Không truyền filter giữ luồng recovery toàn bộ pending như trước.

CLI in batch ID, discovery/export status, counts và paths. Exit 0 complete, 2 partial/busy, 1 failed/config/DB/export, 130 interrupt; phân biệt busy bằng status, không chỉ exit code. Public HTML discovery khiến batch có tour rows vẫn **partial/exit 2**, dù mọi selected detail đều accepted complete; delta vẫn chứa business changes hợp lệ, journal ghi coverage limitation. Explicit-empty discovery là partial, nhưng nếu không cards/batch-tour rows thì batch có thể failed/exit 1 theo semantics hiện có. Complete per-tour không có nghĩa complete catalogue.

### XLSX batch

Output là bundle `exports/domestic/<batch-id>/{delta.xlsx,full.xlsx,.bundle.json}`. Cả hai workbook có sáu sheet legacy và **Nhật ký tải**, metadata availability/change/quality/last-success và IDs liên kết. Full gồm catalogue trong scope, cả unavailable và pending; timestamp snapshot và journal cho biết dữ liệu cached chưa tải lại. Delta chỉ có business changes new/updated/unavailable/reactivated; attempts partial/failed được ghi trong journal, không xuất thành dữ liệu thay đổi thành công. Unchanged/skipped không nhân dữ liệu trong delta; journal vẫn ghi lượt tải.

Current accepted complete data không bị thay bằng attempt lỗi. Nếu chưa có complete baseline, freeze chỉ chọn snapshot **partial thuộc lịch sử entity và khớp source ID đã biết**, mới nhất theo capture time (không theo replay time). Không có partial hợp lệ thì full hiển thị pending, số lịch unknown/null, không children giả. Complete snapshot bị reject identity/publication không được dùng làm display fallback dù parser báo complete; run/raw/snapshot và attempt errors vẫn được giữ để điều tra qua journal/DB. Không ghép các lần chạy để báo đủ, không biến partial display thành business baseline. Metadata và exact snapshot IDs được đóng băng mỗi batch, nên export batch cũ sau thay đổi catalogue vẫn dùng dữ liệu lịch sử đúng batch.

Hai workbook ghi vào thư mục tạm rồi publish **cùng một atomic directory rename**, manifest kiểm tra hash. Lỗi file không rollback dữ liệu DB và không publish nửa bundle; export failure được lưu riêng. Bundle đã publish nhưng DB ack thất bại được dùng lại offline. Trước build hoặc reuse, exporter kiểm display snapshot bằng frozen state: complete phải đúng accepted ref, partial phải đúng identity; không query catalogue mới để chọn lại dữ liệu. Frozen refs không hợp lệ báo lỗi ngay cả khi bundle hashes khớp, giữ nguyên snapshot/refs/bundle bytes và cần review/repair riêng; đổi output directory không khắc phục ref sai. Bundle bị hỏng hoặc schema export không tương thích sẽ báo lỗi, không ghi đè: export sang thư mục mới sau khi kiểm tra. Các giới hạn text/precision/formula của Excel legacy vẫn áp dụng.

**Receipt publication:** acknowledgement thành công đầu tiên là canonical và không bị re-export/reuse hoặc lỗi ở destination khác ghi đè `export_manifest`, `export_status`, `export_errors`. SQL conditional update bảo vệ cả stale/concurrent callers. Export copies vẫn trả paths của destination yêu cầu, nhưng không retarget receipt cũ. Saved receipt sai schema/frozen digest/files/directory bị reject, không tự repair. Publication đầu lỗi hoặc publish-before-DB-ack vẫn recovery như trước. Directory của primary bundle cần lưu bền vững; fix không tự restore receipt lịch sử đã đổi trước đó.

### Scheduler local và Compose

Mặc định **00:00 `Asia/Ho_Chi_Minh`**. Crawler chỉ hỗ trợ timezone **`Asia/Ho_Chi_Minh` (UTC+7, không DST)**; `SCHEDULE_TIMEZONE` khác giá trị này bị từ chối, kể cả alias UTC+7. `SCHEDULE_TIME=HH:MM` và `SCHEDULER_POLL_INTERVAL` vẫn cấu hình được. Khi máy/container khởi động sau nhiều ngày, chỉ chạy **một slot gần nhất đã đến hạn**, không chạy bù từng ngày; DB lưu slot đã xử lý. Partial/failed có checkpoints cũng hoàn tất slot, không retry detail liên tục cùng ngày; crash được recovery. Busy chưa nhận slot thì thử lại poll sau. Scheduler reconnect/backoff khi DB/network lỗi.

```bash
# Local foreground, Ctrl-C để dừng:
.venv/bin/hanoitourist schedule

# Compose default vẫn chỉ PostgreSQL; crawler chỉ bật khi chọn profile:
mkdir -p exports
docker compose --profile scheduler build crawler
# DB init trước khi bật daemon (không tự migrate trong scheduler):
docker compose --profile scheduler run --rm crawler hanoitourist db init
docker compose --profile scheduler up -d crawler
docker compose logs -f crawler
# Đổi lịch trong .env, rồi recreate service:
docker compose --profile scheduler up -d --force-recreate crawler
# Dừng daemon, không xóa volume:
docker compose stop crawler
```

Dùng đúng project Compose đang chứa DB; với DB kiểm chứng đã có, thêm `-p hanoitourist-crawler-verify` vào các lệnh trên và đặt `POSTGRES_PORT=55432`. Không tạo project/volume khác rồi tưởng là đang tái sử dụng lịch sử. `DATABASE_URL` dùng localhost cho Python trên host; **`SCHEDULER_DATABASE_URL` dùng `postgres:5432` trong Compose**, cần cập nhật theo `POSTGRES_*` nếu đổi credentials (percent-encode ký tự đặc biệt). Secrets chỉ ở `.env`, không trong image. Host exports bind mount là `./exports` → `/app/exports`; image chạy non-root, cần quyền ghi. Nếu lỗi quyền, đặt `CRAWLER_UID`/`CRAWLER_GID` theo `id -u`/`id -g` và sửa quyền thư mục, không chạy `down -v`.

**Chưa bật unattended scheduler/auto-publication:** Docker/container và live fresh-reuse acceptance còn thiếu; không coi các lệnh trên là nghiệm thu deployment. Pipeline nhận batch partial phải xét accepted per-tour changes và coverage limitation riêng.

Defaults bổ sung: discovery 300 giây/50 listing pages, batch 21.600 giây; điều chỉnh `DISCOVERY_TIMEOUT`, `MAX_LISTING_PAGES`, `BATCH_TIMEOUT`. Live pilot dùng override `REQUEST_INTERVAL=3`, `NAVIGATION_TIMEOUT=30`, `DISCOVERY_TIMEOUT=300`, `RUN_TIMEOUT=120`, `BATCH_TIMEOUT=600`; không đổi global defaults. Rate limiter dùng chung cả batch; giữ giới hạn từng tour/robots và chỉ đọc AJAX đã whitelist. Máy ngủ/laptop tắt không thể crawl: catch-up chỉ xảy ra sau khi Docker/scheduler chạy lại. Không phải dịch vụ HA hay hệ thống tự bypass truy cập.

## Chạy thủ công legacy

```bash
.venv/bin/hanoitourist crawl
# Browser hiện ra để quan sát; cũng có thể cần màn hình đồ họa:
.venv/bin/hanoitourist crawl --headed
# Xuất vào thư mục khác:
.venv/bin/hanoitourist crawl --output-dir exports
```

Mỗi `crawl` tạo UUID mới, kể cả URL/body không đổi. CLI in run ID, trạng thái, parser version, số lịch/quan sát/ô giá và đường dẫn JSON.

```bash
# Bóc tách lại chỉ từ staging, không mở browser hoặc truy cập website:
.venv/bin/hanoitourist parse --run-id <UUID>
# Xuất lại snapshot đã lưu:
.venv/bin/hanoitourist export --run-id <UUID>
.venv/bin/hanoitourist export --run-id <UUID> --parser-version 0.1.0
# Excel từ snapshot đã lưu, không crawl/parse lại:
.venv/bin/hanoitourist export --run-id <UUID> --format xlsx
.venv/bin/hanoitourist export --run-id <UUID> --format xlsx --parser-version 0.1.3
```

Output: `exports/<run-id>/<parser-version>.json`. Run chưa parse được có `report.json` chứa lỗi và raw manifest, `snapshot: null`. JSON có tiếng Việt, raw labels, thời điểm, provenance và coverage; không embed toàn bộ raw body.

Excel: `exports/<run-id>/<parser-version>.xlsx`, một workbook sáu sheet: **Danh sách tour**, **Nội dung tour**, **Lịch trình**, **Lịch khởi hành**, **Giá chi tiết**, **Ảnh và link**. Mỗi ngày/mục nội dung (kể cả biến thể) một dòng, toàn bộ text trong một ô, không chia đoạn. Hai mục tổng hợp lịch trình/lịch khởi hành có sheet chuyên biệt. Tên tour trên từng sheet; ID liên kết ở cột ẩn, có bộ lọc và cố định tiêu đề. Ngày/giờ hiển thị theo Việt Nam; giá raw và số tiền cùng được giữ, ô giá chung tô vàng và chỉ có một dòng. Booking không gắn hyperlink, không tải ảnh. JSON vẫn là bản đầy đủ HTML/provenance/coverage.

`export` mặc định JSON; lựa chọn `--format xlsx` chỉ áp dụng cho lệnh export, không thay output JSON của crawl/parse. Run chưa có snapshot cần JSON report, không tạo workbook giả. Export Excel ghi atomic; lỗi không xóa snapshot/file cũ. Excel giới hạn chiều cao dòng nên ô dài có thể cần mở thanh công thức để xem hết; exporter không cắt text. Ô vượt 32.767 ký tự hoặc có ký tự XML không hỗ trợ sẽ báo lỗi, hãy dùng JSON. Tiền vượt precision 15 chữ số của Excel được lưu dạng text chính xác; text nguồn được ghi như chuỗi, không chạy formula. Excel chỉ giữ timestamp tới millisecond; JSON/DB vẫn giữ precision gốc.

- Exit **0**: complete theo coverage của các thành phần tour quan sát được.
- Exit **2**: partial, xem warnings; không dùng để khẳng định lấy đủ.
- Exit **1**: failed/configuration/DB/export failure.
- Exit **130**: bị ngắt; staging đã commit được giữ.

Chạy lại `parse` cùng run/version trả snapshot hiện có; thay đổi logic parser phải tăng `PARSER_VERSION` để tạo snapshot mới. `export` mặc định chọn snapshot có `parsed_at` mới nhất, in rõ version. Không trộn các version. Parser failure được ghi vào run và không xóa raw; lỗi export không rollback snapshot đã commit.

Package/collector hiện tại là **`0.2.0`**; parser là **`0.1.5`** để lưu media variants/provenance đúng và nhận empty-result trong schedule scope đã render. Replay thủ công run cũ tạo snapshot `0.1.5` cạnh các version cũ, không sửa hoặc tự chấp nhận lại dữ liệu lịch sử. Nâng package cần `db init` để thêm schema incremental; migration không đổi tám bảng lịch sử. `run.errors` giữ lịch sử: capture errors legacy là strings; parse failures mới là records có `stage`, `parser_version`, `error_type`, `occurred_at`, `message` an toàn. Parser nhận diện cả format lỗi parse legacy. Lỗi parse lịch sử không làm snapshot mới thành `partial` nếu capture đầy đủ và lần parse hiện tại thành công; lỗi capture/không phân loại được vẫn được xét bảo thủ.

## Raw, dữ liệu động và ngữ nghĩa giá

- **HTTP source mới (`guarded-v1`)**: response của đúng request đã được policy duyệt, fetch một hop bằng Playwright trước khi đưa cùng status/body non-redirect cho browser render. URL/status/bytes đã giải nén là phản hồi thật, không đổi redirect thành 200. **DOM**: HTML sau JavaScript và các tương tác. **AJAX**: response lịch (Drupal commands `openDialog`/`insert` chứa HTML), cũng fetch single-hop. Run/raw lịch sử trước guard giữ nguyên acquisition cũ.
- Raw `BYTEA` giữ response body đã giải nén hoặc DOM được serialize, kèm SHA-256/URL/state/time; không phải wire packet archive. HTTP/DOM toàn trang có thể chứa menu/footer, nhưng parser không đưa chúng, quảng cáo, chat hay tour gợi ý vào dữ liệu tour.
- Raw được commit theo checkpoint **trước** parser. Parser chỉ đọc staging. Kết quả không có lịch chỉ được chấp nhận khi có element empty-result thật trong schedule scope đã hỗ trợ, kể cả DOM checkpoint hoặc HTML trong AJAX thành công; marker ở footer/script hoặc response lỗi không chứng minh lịch rỗng. Không tải ảnh/video/JS/CSS để archive; browser có thể tải resource cần render. Ảnh chính, thumbnail, URL lazy-load và icon phương tiện được phân loại; carousel clones không nhân số ảnh.
- Ngày/giá/số chỗ giữ cả chuỗi hiển thị và giá trị chuẩn hóa. Tiền là Decimal/NUMERIC, JSON amount là chuỗi chính xác, không float. `0đ` vẫn là zero của nguồn, **không suy diễn miễn phí**. Rỗng/missing/parse error không bị đổi thành zero.
- Giá quảng bá sidebar không thay thế giá của từng lịch. Ma trận giá giữ nhãn nhóm tuổi, phụ thu, giảm giá, rowspan/colspan. Ô phụ thu phòng đơn gộp nhiều cột lưu một ô chung; không nhân thành nhiều khoản thu và không tự tính giá thanh toán.
- Giá option trong lịch trình và các vé tham quan không bao gồm vẫn nằm đầy đủ trong text/HTML section, không nhầm thành giá khởi hành.
- Nếu cùng lịch khác giá/số chỗ giữa page/modal/checkpoints, giữ observations riêng, ghi warning; không chọn giá thấp nhất hoặc dùng dữ liệu lần trước để bù.
- Crawl là một khoảng thời gian: run có start/end, mỗi raw/observation có thời điểm riêng. Times được DB lưu `TIMESTAMPTZ`, export ISO 8601. Giờ khởi hành không ghi timezone trên site được diễn giải theo `Asia/Ho_Chi_Minh`, không suy luận đó là giờ chuyến bay.

## Schema và truy vấn

Tám bảng lịch sử: `crawl_runs`, `raw_documents`, `tour_snapshots`, `tour_sections`, `itinerary_days`, `tour_media`, `departure_observations`, `price_cells`. Migration `002_incremental.sql` thêm `domestic_batches`, `domestic_tours`, `domestic_tour_urls`, `domestic_batch_tours`, `scheduler_state` cho catalogue/aliases/attempts/frozen refs/slots. Snapshot/child rows được ghi atomic; raw commits độc lập. Metadata/provenance/coverage dùng JSONB; ngày/giá/ô bảng có bảng và cột quan hệ, không chỉ JSON blob.

```sql
-- Lịch sử các lần chạy
SELECT id, started_at, finished_at, capture_status, parse_status
FROM crawl_runs ORDER BY started_at DESC;

-- Mọi lịch/giá của một run/version (thay UUID và version)
SELECT d.source_schedule_id, d.departure_raw, d.departure_at,
       d.price_from_raw, d.price_from, d.seats, d.observed_at
FROM departure_observations d JOIN tour_snapshots s ON s.id = d.snapshot_id
WHERE s.run_id = '<UUID>' AND s.parser_version = '0.1.4'
ORDER BY d.departure_at, d.source_schedule_id;

-- Các loại giá theo tuổi và ô áp dụng chung
SELECT d.source_schedule_id, p.price_label, p.age_label, p.raw_value,
       p.amount, p.colspan, p.applicable_labels, p.provenance
FROM price_cells p JOIN departure_observations d ON d.id = p.departure_id
JOIN tour_snapshots s ON s.id = d.snapshot_id
WHERE s.run_id = '<UUID>' AND s.parser_version = '0.1.4'
ORDER BY d.departure_at, p.row_index, p.column_index;

-- Đọc nguồn để đối chiếu locator; chỉ decode tài liệu HTML/JSON UTF-8
SELECT sequence, kind, role, captured_at, sha256, convert_from(body, encoding)
FROM raw_documents WHERE run_id = '<UUID>' ORDER BY sequence;
```

Raw và snapshot đã lưu không có API chỉnh sửa/xóa. Run metadata cập nhật được để ghi tình trạng capture/parse. Người có quyền SQL trực tiếp vẫn có thể sửa dữ liệu: không coi đây là hệ thống lưu trữ bất biến chống can thiệp.

## Coverage, an toàn và giới hạn

- Kiểm tra robots từng đường dẫn dữ liệu; robots lỗi/unrecognized thì fail closed, không follow redirect của robots. Không bypass 401/403/CAPTCHA. 429 tôn trọng Retry-After trong time budget.
- Không dựa vào `context.route` để chặn native redirect: mọi HTTP request được phép qua `route.fetch(max_redirects=0,max_retries=0)` có timeout. Chỉ navigation tour/listing chính được coordinator mở từng target đã kiểm tra scope/robots trước request, tối đa 5 hops; giữ transport URL riêng với canonical identity. AJAX/POST/iframe/resources redirect chưa hỗ trợ bị abort và báo không đủ/failed, không đổi method hoặc forward body sang target. Không fulfill 3xx + Location cho browser; `acquisition_version=guarded-v1` và raw/hop refs chỉ thêm cho run mới.
- Context mới mỗi run, không dùng phiên đăng nhập. Chặn booking, comment/tư vấn submit, statistics/analytics/chat. Chỉ whitelist AJAX POST đã xác minh chỉ đọc của đúng lịch tour, không cho POST tùy ý.
- Browser mở mọi accordion và chi tiết giá của từng lịch, mở modal lịch; pager hoặc dropdown hữu hạn riêng lẻ được duyệt trong scope đúng tour, không dùng pager footer. Khi dropdown kết hợp pagination trong bất kỳ option nào, collector dừng unsupported/partial thay vì giả đã lấy đủ các trang của mọi tổ hợp. Điều khiển không hiểu/loop/hết giới hạn báo partial.
- Modal review có bình luận mới hoặc input calendar không hữu hạn/chưa xác minh sẽ báo partial yêu cầu xem lại adapter, không giả vờ đã thu đủ. Tour khảo sát hiện 0 bình luận và không có calendar/filter.
- Browser JS/control lỗi không fallback HTML-only rồi báo complete. Coverage đối chiếu manifest, danh tính lịch, số ô và provenance; không chỉ kiểm tra số lượng.
- `coverage.discovered.schedule_rows` giữ từng dòng lịch quan sát được theo raw document/AJAX command/scope/vị trí, kể cả không nhận diện được booking ID. `parsed_schedule_rows` ánh xạ sang observation. Dòng thiếu/sai booking URL hoặc chưa parse khiến `partial`, không biến mất khỏi inventory; `/booking/`, query và fragment được nhận diện đúng mà không truy cập link booking.
- Discovery ghi nhận outer data rows trước khi đọc ô ngày, không yêu cầu class `ngay-khoi-hanh` để một dòng được nhìn thấy. Nếu ô ngày đổi/mất class hoặc không có ô ngày, ID hợp lệ vẫn giữ observation và các giá/chỗ đọc được, `departure_at: null` cùng warning/provenance và status `partial`; không đoán ngày từ vị trí cột. Header thuần th, nested price rows, detail rows và placeholder rỗng rõ ràng không được tính là lịch. Validator kiểm tra ngày độc lập, không chỉ dựa vào mapping/count.
- Section và itinerary day cùng text nhưng HTML/link khác nhau được giữ thành các variants riêng với provenance riêng. Hai link nội dung cùng nhãn nhưng khác đích có warning. Fingerprint chỉ bỏ qua UI accordion và các class `lazyload`, `lazyloaded`, `ls-is-cached` trên ảnh; HTML lưu trữ, URL, query/fragment, alt/title vẫn giữ nguyên.
- Defaults: navigation 30 giây, run 300 giây, 100 states, 50 pages, 5 MiB/raw document, 50 MiB/run, một browser tuần tự, cách request dữ liệu ít nhất 1 giây, tối đa 2 retry. Chỉnh qua biến trong `.env.example`. Hết giới hạn báo rõ, không truncate raw để báo thành công.
- Complete là coverage những thành phần quan sát được của **URL đã hỗ trợ**, không phải cam kết phát hiện mọi tính năng ẩn của website. Section chưa biết vẫn lưu HTML/text và báo warning cần kiểm tra.

## Tests và đối chiếu web

```bash
# Offline unit, không cần DB hay internet
.venv/bin/python -m pytest -m 'not browser and not postgres' -q
# Browser offline route mocks, cần Chromium
.venv/bin/python -m pytest -m browser -q
# PostgreSQL integration: tự tạo/xóa schema UUID cô lập, không drop DB/bảng hiện hữu
TEST_DATABASE_URL='postgresql://crawler:crawler_local@localhost:55432/hanoitourist' \
  .venv/bin/python -m pytest -q
```

Không đặt `TEST_DATABASE_URL` tới production. Thiếu DB/Chromium thì tests liên quan skip có thông báo; cần thực sự chạy chúng trước nghiệm thu. Pytest không tự gọi site live. Smoke live là thao tác thủ công `crawl` ở trên.

Checklist đối chiếu cùng khoảng thời gian crawl:

1. Mở toàn bộ accordion lịch trình, so sánh title và từng đoạn, bao gồm option/chi phí tự túc.
2. Mở chi tiết **mọi** lịch trên page và modal. So sánh ngày/giờ, chỗ, giá từ và mọi nhóm tuổi/loại giá; ô gộp chỉ tính một lần.
3. So sánh dịch vụ bao gồm/không bao gồm, điều kiện/ghi chú kể cả rỗng, ảnh chính/thumbnail, sidebar và review summary.
4. Xem warnings/provenance/captured_at; nguồn khác nhau phải hiện observation riêng.
5. Crawl lần hai: run UUID mới, raw/snapshot lần một còn nguyên. Replay từ DB khi mạng bị chặn: vẫn parse được và không đổi captured_at.

Fixtures được thu từ browser/HTTP/AJAX ngày 05/10/2026; thời điểm ghi fixture nguồn là `2026-10-05T13:51:37.855827+00:00`, URL trong `expected_snapshot.json`. Form token biến động được thay bằng placeholder chỉ trong fixtures. Expected ngày/giá/4 ngày/6 ảnh chính được đối chiếu với UI khi khảo sát, không sinh từ parser. Những con số này chỉ là baseline test, không hardcode vào crawler.

### Kết quả nghiệm thu lịch sử `0.1.1` — 05/10/2026

- **62 tests passed, 0 skipped**, gồm browser route mocks và PostgreSQL schemas cô lập; `compileall` qua. Package/migration đã thử chạy từ ngoài thư mục repo.
- Hai lần crawl live đều `complete`: run `7bdaa0a1-1fad-4ffb-b27d-bcc57cbf855a` và `b2a2155a-1cf3-4793-8621-618391575e7c`.
- Mỗi run có 9 lịch, 9 observations và 117 ô giá (13 ô/lịch). Reader browser độc lập đã đối chiếu từng ngày/giá/chỗ/loại giá/colspan/rowspan trên page và modal; nhãn 4 nhóm tuổi khớp mọi ô.
- Đã đối chiếu 4 ngày lịch trình, các section dịch vụ/điều kiện/ghi chú/media, giá sidebar và 6 URL ảnh chính; JSON ngày/giá/ô giá khớp snapshot DB.
- Replay run đầu với HTTP/browser bị chặn tạo snapshot parser `0.1.1` từ raw, giữ snapshot `0.1.0` và không đổi raw IDs/hashes/captured_at hay thời điểm run. Run thứ hai vẫn là lịch sử riêng.
- File xem thử: `exports/b2a2155a-1cf3-4793-8621-618391575e7c/0.1.1.json`. Đây là dữ liệu tại thời điểm crawl, không phải giá cố định hoặc cam kết đặt chỗ hiện tại.

### REVIEW và kiểm chứng fix `0.1.2` — 05/10/2026

REVIEW sau nghiệm thu phát hiện 3 major chưa được tests cũ bao phủ: booking href có slash cuối bị bỏ sót mà vẫn complete; gộp section theo text làm mất link thay đổi; parse error cũ làm replay thành công bị partial. Các kết quả `0.1.1` bên trên là lịch sử happy path, không phải xác nhận không có 3 regressions này.

- Regression mới trên `0.1.1`: **23 failed, 2 passed** trong nhóm tái hiện được chọn. Sau fix: **113 tests passed, 0 skipped**, gồm browser mocks và PostgreSQL integration; `compileall` qua. Package installed, parser và collector đều `0.1.2`.
- Whitelist class trạng thái tải ảnh được người dùng chấp thuận để không nhân 4 ngày thành 8 chỉ vì JavaScript đổi class; regression xác nhận URL ảnh thay đổi vẫn giữ hai variants và HTML gốc không bị sửa.
- Live run cuối `6d15ebf8-92ea-416f-8ade-2072cbe1de3d`: **complete**, 9 lịch, 9 observations, 117 price cells, không warning. Reader browser độc lập đối chiếu page và modal: mọi date/seats/price-from, nhãn loại giá/nhóm tuổi, raw value và rowspan/colspan đều khớp; 4 ngày, các section nội dung, sidebar và 6 URL gallery chính khớp.
- JSON run cuối khớp snapshot PostgreSQL: `exports/6d15ebf8-92ea-416f-8ade-2072cbe1de3d/0.1.2.json`.
- Replay staging lịch sử run `b2a2155a-1cf3-4793-8621-618391575e7c` với HTTP/browser bị block tạo snapshot `0.1.2` complete, 9 lịch/117 ô. Raw IDs/body/hash/captured_at, start/end/capture metadata và snapshot `0.1.1` giữ nguyên; reparse cùng version idempotent. Export mới nằm cạnh export cũ.
- Trong vòng kiểm chứng trước fix cuối có run `57444926-9bce-4e63-84ff-1bf86c3b6d89` partial do alias provenance giữa row descriptor và observation. Đã tách danh sách provenance và thêm regression so inventory collector/parser; run/snapshot thử nghiệm đó vẫn được giữ nguyên để chẩn đoán, không nâng trạng thái hoặc ghi đè nó. Snapshot `0.1.2` tạo sớm trên run `7bdaa0a1-1fad-4ffb-b27d-bcc57cbf855a` cũng được giữ; replay cuối nêu trên dùng run lịch sử chưa có snapshot version mới.
- CodeGraph đã sync sau source/test changes. Không đổi schema, không sửa fixtures lịch sử, không truy cập booking hoặc submit form qua crawler.

### Follow-up Major 1 — kiểm chứng `0.1.3`

REVIEW `0.1.2` tái hiện một trường hợp còn sót: đổi class ô ngày làm inventory và parser cùng bỏ dòng; nguồn có 9 rows/booking links nhưng kết quả 8 schedules vẫn complete. Bản `0.1.3` tách discovery khỏi selector ngày và thêm kiểm tra completeness độc lập.

- **8 regression mới fail trước fix, pass sau fix**. Full suite: **124 tests passed, 0 skipped**, gồm 8 browser tests và PostgreSQL integration; `compileall` qua. Parser/collector/package đều `0.1.3`.
- Đổi/xóa class hoặc xóa ô ngày: giữ đủ 9 descriptors/observations của fixture, observation ID hợp lệ vẫn giữ 13 ô giá, nhưng ngày nullable và snapshot partial. Test xóa warnings parser xác nhận validator vẫn tự phát hiện thiếu ngày; DB/JSON giữ descriptor, provenance và warnings chứa nguồn lỗi.
- Không thay logic M2/M3, schema, request policy hoặc fixture gốc. Các tests cũ vẫn qua. CodeGraph đã sync.
- Replay run `57444926-9bce-4e63-84ff-1bf86c3b6d89` với HTTP/browser bị block tạo snapshot **`0.1.3 complete`**, 9 lịch/117 ô giá. Snapshot `0.1.2 partial` và raw IDs/body/hash/captured_at, thời điểm/capture metadata/history giữ nguyên; reparse cùng version idempotent. Export: `exports/57444926-9bce-4e63-84ff-1bf86c3b6d89/0.1.3.json`.
- Live run **`09b038a0-b763-42c8-84f4-e2b6793510af`** complete, 9 lịch, 9 observations, 117 ô giá. Reader browser độc lập đối chiếu page/modal: ngày/chỗ/giá/nhãn nhóm tuổi/rowspan/colspan khớp; 4 ngày lịch trình khớp. JSON khớp snapshot PostgreSQL: **`exports/09b038a0-b763-42c8-84f4-e2b6793510af/0.1.3.json`**.
- Kết quả live là dữ liệu tại thời điểm kiểm chứng, không phải số lịch/giá cố định được hardcode vào crawler.

### Follow-up minor — placeholder FooTable `0.1.4`

REVIEW `0.1.3` phát hiện placeholder `<tr class="footable-empty"><td colspan="6">Không có lịch khởi hành</td></tr>` bị coi là một dòng lịch thiếu booking link, gây partial sai. Discovery nay nhận marker trên cả dòng và ô, nhưng chỉ loại placeholder một ô không có dấu hiệu dữ liệu lịch; booking/date/price hoặc bảng giá thật vẫn giữ descriptor.

- Nhóm tests mục tiêu trước fix: **2 failed, 5 passed**; sau fix: **7 passed**. Bao phủ placeholder cấp dòng/cấp ô, empty-result complete với marker `view-empty` hợp lệ và bảo vệ dòng có dữ liệu thật.
- Full suite: **130 tests passed, 0 skipped**, gồm browser mocks và PostgreSQL schemas cô lập; `compileall` qua, CodeGraph đã sync. Package/parser/collector đồng bộ `0.1.4`, đã reinstall bình thường trước chạy suite.
- Không đổi validator, schema, dependencies, request policy hoặc M2/M3. Không sửa fixtures, raw/snapshot/export lịch sử; replay theo version mới tránh dùng snapshot cached `0.1.3`.
- Không chạy lại crawl live hoặc tạo snapshot mới trên các run lịch sử trong follow-up minor này; kết quả live `0.1.3` phía trên giữ nguyên như lịch sử kiểm chứng.

### Export Excel chính thức — package `0.1.5`

- CLI thêm `export --format xlsx`, mặc định JSON không đổi. Parser/collector giữ `0.1.4`; thêm dependency `openpyxl>=3.1,<4`, không đổi schema hoặc scope crawl.
- Regression CLI mới fail trước triển khai vì chưa nhận `--format xlsx`; sau triển khai nhóm unit Excel/CLI **19 passed**. Full suite **143 passed, 0 skipped**, gồm browser mocks/PostgreSQL; `compileall` qua, CodeGraph đã sync. Source installed khớp repo.
- Tests kiểm tra nội dung gộp không chia đoạn, variants và links, null/xung đột observations, ô chung không nhân giá, numeric money/precision fallback, nguồn bắt đầu bằng dấu công thức vẫn là text, giới hạn ô/ký tự lỗi không truncate, version selection và atomic failure giữ file/DB/JSON/history.
- Lệnh CLI thực tế trên DB read-only đã xuất `exports/09b038a0-b763-42c8-84f4-e2b6793510af/0.1.3.xlsx`: sáu sheet, 4 dòng lịch trình có text nguyên vẹn, 9 observations/117 ô giá, không cột Đoạn. DB/raw và các export JSON/CSV/Excel mẫu cũ giữ nguyên; không crawl/parse lại hoặc tạo snapshot mới.

### Incremental `0.2.0` và redirect guard — kết quả kiểm chứng hiện tại

- Baseline Git trước implementation: **`cf915e3e3b8df8483d6b7a72a57a9accd691bce8`**; không amend/push, không commit secrets/runtime exports/DB data.
- Native redirect regressions bằng server HTTP loopback thật: **17 failures trước fix**, đích booking/admin/comment/cross-authority vẫn bị server nhận; sau fix đích bị cấm nhận **0 requests**. Bộ kiểm tra còn bao phủ allowed chains/final URL/raw refs, gzip/cookies, POST/iframe/script/fetch redirect, loop/hop cap/missing Location/robots/raw limits/timeout, và timeout chứa chuỗi ERR_ABORTED trong URL không được nuốt thành redirect thành công.
- Full suite sau reinstall source cuối: **301 passed, 0 skipped**, gồm Chromium/browser mocks, native HTTP và PostgreSQL schemas UUID cô lập trên `55432`; `compileall` và `uv pip check` qua. Không có lint/typecheck script riêng trong project. Package/collector `0.2.0`, parser `0.1.4`; package bytes đã đối chiếu source và migrations 001/002, CLI help/db init đã kiểm tra ngoài repo.
- Kiểm tra state/DB giữ baseline khi blocked/partial, final primary 404/410 vs non-primary, identity mismatch, alias merge không new giả, hash trước/sau PostgreSQL/UTC, 7-day boundary/catch-up/locks/recovery. Legacy parser/JSON/XLSX và historical fixtures không đổi.
- Hai smoke batches trước guard (`16cd7976-4092-4b36-96a2-12235fa74a4a`, `72766f6b-6a07-4d10-a5fd-652f88337dd4`) đều partial; không tính chúng như nghiệm thu guard. Script audit trước hai lượt đó đã thất bại, không có before/after proof hợp lệ cho chúng.
- Guarded smoke mới **`49ec6ff7-56a3-452e-a1b9-9aec8a79bf81`**: `partial`, discovery không terminal vì listing JavaScript error; hai detail attempts partial do timeout trong budget ngắn (batch 90s/run 30s/discovery 45s). Không bỏ qua JS/control/access errors để báo complete. Cả discovery/detail có guard, không ghi nhận redirect trong các run này; đây không phải nghiệm thu toàn catalogue live.
- Before/after audit của guarded smoke hợp lệ: **17 preexisting runs, 376 raw documents, 19 snapshots và frozen rows của 2 batches giữ nguyên**, gồm raw body hash/metadata/time và toàn bộ snapshot contents. Hai workbook mới publish thành công, mỗi workbook 7 sheets: full 10 summary tours, delta không có business changes (0 summary tours), journal vẫn ghi partial/errors. Bundle cũ re-export cùng destination giữ hash; export offline sang directory mới sau batch mới có nội dung từng sheet khớp lịch sử.
- Compose config hợp lệ và scheduler profile tách khỏi default PostgreSQL. **Image/container smoke chưa nghiệm thu**: build đầu timeout 900s khi tải Chromium; retry build thất bại khi apt không resolve được `deb.debian.org`/`debian.map.fastlydns.net` (exit 17 của Compose, apt exit 100). Không có build còn chạy; không sửa DNS hệ thống hoặc teardown DB/volume. Cần build source cuối và kiểm tra Chromium/ZoneInfo/export permissions/init/restart slot trong container trước khi coi triển khai Docker là đã nghiệm thu.
- CodeGraph đã sync source/tests; `affected` xác định 14 test files chịu ảnh hưởng, đều nằm trong full suite; `git diff --check` sạch và parser/validator/normalize/errors/JSON exporter/migration 001 không đổi so với baseline. Live JS lỗi và Docker build chưa hoàn tất được báo riêng, không dùng để che regression offline hoặc tuyên bố capture đủ.

### Follow-up REVIEW — 3 major incremental `0.2.0`

REVIEW tái hiện ba lỗi ngoài suite 301 tests: diagnostic main-frame 404 làm unavailable giả; full xuất complete snapshot bị reject identity khi chưa có accepted baseline; alias merge khiến canonical bị fetch lại hoặc outcome bị ghi đè skipped từ state stale. Kết quả 6R bên trên là lịch sử kiểm chứng redirect, không chứng minh không có ba lỗi này.

- Ba repro đã được lưu vào repo, parameterize thành **6 failed trước fix** (404/410; rejected 8140→9900; alias-before-canonical due/fresh/missing). Sau fix đều green. Targeted collector/navigation/catalogue/incremental/export suite: **147 passed, 0 skipped**.
- Full verification sau reinstall thường: **350 passed, 0 skipped trong 233.68s**, gồm Chromium offline mocks, native HTTP loopback và PostgreSQL schema UUID tại 55432. Offline-only **202 passed, 148 deselected**; browser selection có DB env **61 passed, 289 deselected**. Đây là kết quả mới sau fix, không tái sử dụng counts 301 trước sửa.
- Collector giữ unowned/unstaged responses ở diagnostic raw/errors, không append navigation hoặc bind node ID; chỉ guard-owned entries thêm owned/terminal flags. Publication kiểm flags bằng boolean thật và exact raw evidence, không tìm lùi 404 cũ. Thiếu/sai flags hoặc raw ref/run/method/role/status/URL không unavailable. Direct/approved-chain final 404/410 vẫn hoạt động cả normal publication và recovery, đúng một lần; forbidden HTTP server destinations vẫn nhận **0 requests**.
- Frozen display chỉ accepted complete hoặc partial đúng entity/identity; capture-order thắng replay-order, rejected complete cùng/sai node không thành fallback. Export kiểm refs trước build/reuse; invalid already-published bundle không overwrite, valid historical partial/complete re-export vẫn giữ refs/nội dung. First accepted complete sau partial vẫn new, failures không kéo watermark/fingerprint. Null display giữ pending/unknown schedules.
- Listed/absence selection reload active state, skip attempted/superseded IDs; alias đã verified không gây target capture thêm hoặc overwrite terminal outcome. Alias thất bại/partial không merge; unrelated tours vẫn được xử lý; canonical trước unknown alias vẫn có request verification cần thiết. Recovery sau persist/identity rejection và frozen reuse được kiểm tra bằng raw hashes/body/times, snapshot/children, refs và workbook contents trong schema/tmp directory cô lập.
- `compileall`, AST source/tests, `uv pip check`, source/installed **23 Python/SQL files**, packaged migrations 001/002 và versions đều qua. CLI help và export-batch/reuse thực tế từ ngoài repo trên schema tạm: 7 sheets, 117 price cells, raw/run/snapshot/frozen refs giữ nguyên. Không migrate hoặc replay trên DB lịch sử thật trong follow-up.
- CodeGraph sync **9 source/test files**; `affected` xác định **13 test files**, tất cả được full suite chạy. `git diff --check` sạch; parser/validator/normalize/errors/JSON exporter/migration 001 giữ nguyên baseline. Chỉ 4 source files, 5 test files và README được sửa; không thêm schema/config/dependency/CLI, không đổi parser/package/collector versions.
- **Không chạy crawl live, không bật scheduler, không retry Docker build trong follow-up.** Compose default vẫn chỉ postgres. Image/container smoke và whole live catalogue vẫn chưa nghiệm thu như blockers 6R bên trên; không sửa DNS hoặc xóa DB/volume để né lỗi. Fix ba major không đồng nghĩa hoàn tất triển khai Docker/live.

### HTML discovery best-effort — implementation và live pilot

- Thêm adapter chỉ trong `discovery.py`; giữ browser `DiscoveryCollector`, detail collector/parser/guard/storage/selection/export source, schema và versions. Fixtures mới chỉ dùng cho HTML regression, không sửa historical fixtures.
- Root tests trước source fix: **3 failed, 1 passed**; trong đó integration fixture sau đó được sửa source ID trùng gây legitimate merge, không tính collision này như lỗi production. Source-only và empty-result regressions thật chuyển green. Browser dynamic render/JS-error-no-fallback tests giữ nguyên.
- Verification mới: targeted **186 passed** trước nhóm detail/recovery bổ sung; offline **218 passed / 215 deselected (24.91s)**; browser/native với DB **128 passed / 305 deselected (170.68s)**; full **433 passed, 0 skipped (325.18s)**. Native server counters chứng minh forbidden redirects/core/subresources/iframe không nhận requests; kiểm ownership boolean/ref/run/method/status/URL, gzip/cookies, bounds/retries/meta-refresh và repeated cleanup. Actual partial wrapper integration chứng minh accepted details, fresh reuse, no absence probes, qualified detail 404/410, baseline/TTL/frozen recovery/XLSX/CLI semantics.
- Discovery-only live run **`f8a89f32-fcc4-488c-9e8c-e2996338fa22`**: 7 pages, 69 card occurrences, 60 observed unique URLs, 9 repeats; **partial, terminal false, coverage false**, source pager end observed. 21.83s acquisition; chỉ 7 primary document fetches, 0 script requests, 1 robots + 7 HTTP raw, không listing DOM. Audit hợp lệ trước network: mọi old rows trong 13 tables/catalogue/snapshots/children/frozen refs và old artifacts giữ nguyên; chỉ +1 run/+8 raw. Đây không phải catalogue-complete proof. Gate A lịch sử vẫn **NOT_PASSED_STOP**.
- Controlled pilot batch **`2c491bdc-6f14-4278-aa4b-84c6b5e9d5fc`**: partial/discovery incomplete, export complete; **9 accepted new / 8 partial / 1 failed / 36 skipped**. Full/delta đều 7 sheets, delta giữ 9 accepted new tours, journal ghi limitation/errors. Shared interval 3s, discovery 300s, detail 120s/run, batch acquisition budget 600s; whole command gồm finalization/export mất 614.22s, không coi 600s là hard wall-clock SLA.
- Pilot giữ historical run/raw/snapshot/children/old batch rows/frozen refs và readable artifact bytes. Catalogue/alias metadata được cập nhật theo observations như dự kiến; prior accepted baselines không bị thay bởi failed/partial. Thêm 19 runs/282 raw/17 snapshots; không sửa lịch sử để nâng quality.
- **Final verification chưa PASS:** re-export offline batch lịch sử `49ec6ff7-56a3-452e-a1b9-9aec8a79bf81` sang thư mục tạm đã cập nhật `domestic_batches.export_manifest` theo side effect của exporter hiện có. Manifest chuyển directory/hashes sang bundle re-export; frozen digest không đổi và workbook contents so sánh khớp. Audit live phía trên được lưu trước thao tác này, không dùng nó để claim toàn bộ DB vẫn unchanged sau re-export. Không sửa exporter ngoài scope hoặc tự restore metadata; cần amendment cho yêu cầu history audit/re-export trước khi hoàn tất. CLI reuse bundle pilot từ ngoài repo trả exit 2 đúng partial semantics. AST/full-history verification cuối bị dừng ở assertion `domestic_batches`, không báo PASS.
- **Dừng pilot trước batch hai:** một số detail controls timeout, một attempt guarded acquisition error và batch chạm budget; không khẳng định nguyên nhân mọi lỗi là Modernizr. Accepted per-tour data được giữ, nhưng **live fresh-reuse và whole detail acceptance chưa hoàn tất**. Fresh reuse chỉ mới PASS trong integration tests. Không tăng scope, bypass robots/access, substitute listing cards hoặc ép detail complete. Docker/container/external pipeline/scheduler enablement vẫn chưa nghiệm thu; không bật scheduler trong amendment này.

### Corrective follow-up theo yêu cầu fix major

- **Receipt major đã fix:** validate saved receipt trong Excel exporter và SQL atomic first-receipt guard trong Storage. Secondary success/failure/corrupt-copy/reuse, duplicate wrapper acknowledgement, stale callers và 2 concurrent exports không thay primary receipt/status/errors. Invalid saved receipt fail closed, không rewrite lịch sử. First-publication failure/ack recovery tests vẫn PASS.
- **Unsupported detail scope:** 7 timeout runs của pilot có HTTP 200 nhưng schedule scope không chứa date/booking/explicit-empty. Collector nay checkpoint DOM rồi báo `Unsupported schedule scope`/partial nếu không có supported view/table/empty marker, thay vì chờ rows tới timeout. Supported dynamic skeleton vẫn chờ rows và tests dynamic/modal vẫn PASS. Không convert unknown empty thành complete hoặc unavailable.
- Regression trước fix được lưu; targeted exporter/catalogue/incremental/CLI 118 passed (176.24s), collector/export/incremental sau gate 100 passed (226.37s). Full suite **449 passed, 0 skipped (410.93s)**; reinstall, dependencies, AST/compileall, 23 installed-source Python/SQL bytes và diff checks PASS. CodeGraph affected 13 test files, đều được full suite chạy.
- Re-export actual old batch `49ec6ff7-56a3-452e-a1b9-9aec8a79bf81` với new code: saved before/after audits chứng minh **all rows của 13 tables và original export bytes unchanged**. Audit tại `/var/folders/x3/r0q1jpvj079ghqhkjqf5hgxh0000gn/T/hanoitourist-receipt-fix-audit-1nvgnjsj/`; copies ngoài repo. Không khôi phục metadata đã bị đổi trong verification cũ, không backfill kết quả cũ thành PASS.
- **Ở mốc corrective trước đối soát:** chưa independent UI reconciliation cho 9 tour accepted; 1 acquisition Error có raw 8 lịch chưa rõ nguyên nhân, 1 budget-exhausted attempt chưa nghiệm thu lại. Chưa chạy thêm live browser, chưa có live proof cho fail-fast gate, chưa bật scheduler/container/publication. Tests/archived raw diagnostics không thay đối soát UI hoặc production acceptance.

### Independent live UI reconciliation — 9 accepted tours

- Đã đọc UI live ngày **2026-10-06**, hoàn tất từng tour trong khoảng **16:53:47–16:56:55 UTC** (23:53:47–23:56:55 giờ Việt Nam), đối chiếu 9 accepted snapshots của batch `2c491bdc-6f14-4278-aa4b-84c6b5e9d5fc`. Baselines capture khoảng 13:57–14:06 UTC cùng ngày; đây không phải replay UI tại đúng thời điểm capture cũ.
- Reader diagnostic ngoài repo tái sử dụng robots/guarded transport/read-only control traversal, **không gọi production parser/inventory để đọc hoặc so sánh giá trị**. Đọc trực tiếp browser DOM/visible text của page, từng itinerary accordion và từng price expansion trên page/modal; 9/9 traversal finished, không acquisition/control/page-JS errors. Session crawl lock giữ suốt network; saved repeatable-read read-only baseline audit kết thúc trước request đầu tiên. Interval 3s, navigation 30s, mỗi tour tối đa 120s, total network budget 1200s; whole audited operation 213.47s.
- Đã kiểm **24 itinerary days / 85 departure schedules / 1.105 physical price cells**: identity/title/canonical/sidebar metadata, section/day text, transport, media URLs/attributes/roles/order/context, content links, date/time/timezone, seats, price-from, price/age labels, numeric amounts, currency, supplements/discounts và rowspan/colspan/applicable labels. Không lấy sidebar price thay matrix; ô single-room colspan=4 giữ một physical cell, không nhân thành 4 observations.
- Raw comparison: **9.097 MATCH / 81 DIFFERENCE / 0 NOT_VERIFIED**; thêm **1.311 MATCH** cho derived duration/seats/currency checks. Toàn bộ schedule/cell checks khớp. 81 differences được annotation riêng, giữ nguyên original reports `NEEDS_REVIEW`: **72 rendered-case differences** ở headings/review (live DOM source text vẫn khớp snapshot); **9 sidebar differences** do source text còn “Đường dẫn”, “Hủy bỏ” từ form không hiển thị, cùng casing UI. Không sửa historical values hoặc quality để PASS. Kết luận là **business fields match with presentation differences**, không phải literal visible-sidebar/HTML-byte equality; `sidebar_text` không nên quảng bá như transcript chính xác của UI visible.
- Audit giữ mọi old row của **13 tables**, toàn bộ catalogue/aliases/watermarks/snapshots/children/frozen batch refs và **31 readable old artifacts** unchanged; 3 missing artifact paths có trước vẫn missing, không manufacture byte proof. Chỉ +9 diagnostic runs/+257 raw (9 robots, 9 primary HTTP, 9 schedule AJAX, 230 DOM checkpoints; 32.82 MB). Diagnostic runs partial/parse pending, không accepted publication; không có batch/export/delta mới. Actual transport trace có **0 core/booking/comment fetches**.
- Evidence private ngoài repo (0700/0600): `/var/folders/x3/r0q1jpvj079ghqhkjqf5hgxh0000gn/T/hanoitourist-ui-reconciliation-62r3bfxv/RECONCILIATION.md`; gồm frozen expected snapshots, per-tour UI/compare JSON/raw refs/timestamps, classified report, 18 screenshots và before/after/hash audit. Image DOM URLs/attributes được kiểm, không chứng minh binary/pixel equality; whitespace được normalize, không certify mọi historical HTML variant.
- Verification sau cập nhật docs: collector/export regressions **56 passed (63.49s)** trong Chromium/PostgreSQL test schemas cô lập; `git diff --check` qua. Không đổi application source trong lượt đối soát, không tái tuyên bố full-suite 449 như một lần chạy mới.
- **Evidence gap đối soát UI của 9 accepted tours đã được xử lý**, với presentation differences được công khai. Không suy ra catalogue complete hay production-ready: node7620 acquisition Error/budget retry/live fail-fast gate, live TTL reuse/expiry, Docker/container/pipeline/scheduler acceptance vẫn mở. Không chạy batch hai hoặc bật scheduler trong lượt đối soát.

### Backlog admission và scoped recovery — 18/10/2026

- Admission thêm ngoại lệ hẹp cho catalogue pending chưa accepted/chưa từng attempt; giữ unavailable, caller superseded/identity/dedup, retry reasons và TTL. `recover_batches` nhận keyword `batch_id` tùy chọn để thao tác có phạm vi; default callers không đổi. Không migration/queue hoặc đổi parser/collector/package versions.
- Regressions trước fix: **5 failed, 15 passed**; sau reinstall thường: offline **231 passed / 238 deselected (23.47s)**, targeted PostgreSQL/browser/native **231 passed (326.51s)**, full **469 passed, 0 skipped (432.84s)**. PostgreSQL tests chạy trên DB riêng `hanoitourist_backlog_test_84a66cecf453`, không trên database dự án. 23 source/installed Python/SQL files, AST, dependencies và diff checks PASS. Runner một lần đã rehearsal 8 scenarios offline (complete/partial/failed/identity/existing accepted/busy/export failure/interruption), redirect scope và resume không recapture.
- Đọc lại trạng thái và kiểm scheduler đã dừng, không còn session DB khác, rồi giữ crawl advisory lock để recovery riêng `e29e4213-7c76-4824-be89-013f12027f6d`: running/details → partial/frozen, export complete. Giữ raw unfinished run Nha Trang; `finished_at` recovery không giả là thời điểm capture cũ. Không suy OOM từ exit 137. Scheduled slot được finalize tới `2026-10-15T17:00:00Z`, không chạy catch-up/scheduler.
- Batch/publication mới **`a5e16a16-ba28-4ab6-bc1e-b182c3238e3a`**, full **69 tours**, delta chỉ hai accepted new Sapa/Hạ Long. Chỉ hai detail runs, không listing discovery: mỗi tour mới **complete, 6 schedules/6 observations/78 price cells**, không warnings/errors, observation ngày 18/10 khoảng 14:43 VN. Hai Hoa Lư reuse đúng accepted refs, **7 schedules/91 cells mỗi tour**, observation vẫn ngày 16/10 khoảng 00:18 VN. Batch partial chỉ rõ không đánh giá lại catalogue coverage; không giảm tiêu chuẩn per-tour complete.
- Workbook: `exports/domestic/a5e16a16-ba28-4ab6-bc1e-b182c3238e3a/full.xlsx`. Đã kiểm bốn summaries/child rows/IDs/timestamps/digest/hash và offline reuse; full gồm **44 complete / 24 partial / 1 pending**, không phải catalogue vừa recrawl. Pending ngoài scope `quy-nhon-tuy-hoa` chưa attempt, không tự tải thêm.
- Audit 13 tables giữ toàn bộ **2.147 old raw documents / 233.711.080 body bytes**, snapshots/children, hai Hoa Lư và frozen history/receipts/bundle cũ; +2 runs/+43 raw/+2 snapshots. Bundle `f6516d97-5756-4fa1-85a3-6f041a83eb53` không backfill hoặc overwrite. Chỉ bốn file source/tests/docs được sửa so baseline đầu lượt; working tree cũ được giữ. Host package đã cập nhật, scheduler image chưa redeploy và daemon vẫn stopped.
- Báo cáo/evidence private: `exports/diagnostics/backlog-repair-e29e4213-20261018T071837Z-85e084/REPORT.md`. Không sửa intro/packages, TTL, timeout hoặc parse/validation policy; không chứng nhận lại freshness toàn catalogue hay UI hiện tại của cached Hoa Lư.

### CLI scoped retry và chẩn đoán — 19/10/2026

**Trạng thái lượt triển khai CLI ban đầu: bị chặn trước live; chưa hoàn tất mức C.** Tính năng mới có trong host package và image riêng, **chưa redeploy scheduler**. Không chạy hai URL live được duyệt vì targeted safety regression không qua; xem blocker bên dưới.

```bash
hanoitourist retry-domestic \
  --source-batch-id <UUID-batch-nguồn> \
  --url https://hanoitourist.vn/<tour-a> \
  --url https://hanoitourist.vn/<tour-b> \
  --output-dir exports

# Dựng lại report từ DB/raw đã commit, hoàn toàn offline; không recrawl.
hanoitourist retry-report --batch-id <UUID-batch-retry> --output-dir exports
```

- Chọn URL từ frozen rows `partial`/`failed` của đúng batch nguồn (hoặc requested URL trong attempts của row), không phải mọi URL hiện có trong catalogue. Toàn bộ input được validate dưới crawl lock trước khi tạo batch/run/artifact. URL/entity được chuẩn hóa và dedup; không `--all`, `--force`, queue hay recovery batch khác.
- Complete baseline cũ không làm mất eligibility. Chỉ skip `newer_success` khi **accepted** snapshot và capture đều complete, watermark khớp capture `finished_at`, và thời điểm đó **strictly lớn hơn** source batch `finished_at`. Replay/parsed_at/export hoặc complete bị reject không phải newer success. Current unavailable được skip, không giả discovery để reactivated.
- Busy thoát ngay (`2`), không artifacts hoặc requests. All-skipped trả `handled` (`0`), chỉ ghi báo cáo invocation private, không batch/run/DB writes. Có capture tạo manual batch mới, không discovery; batch luôn `partial` (`2`) dù từng tour có thể complete. Input/DB/publication/export/report fatal trả `1`, interrupt `130`; argparse syntax `2`.
- Tối đa một detail run mới/entity/invocation; giữ navigation retries/pacing/caps hiện có. Budget acquisition không quá `min(batch_timeout, selected_count × run_timeout)`. Không tự tăng timeout hay đổi parser/validator để ép complete. Primary redirects chỉ tới aliases verified của entity đang capture; publication guard không cho merge ra ngoài selected allowlist hoặc ghi đè identity đã attempted.
- Snapshot và guarded publication commit trong cùng transaction hậu-network; raw/capture checkpoints giữ độc lập. Partial/failed/rejected không thay accepted complete baseline. Source/history không bị sửa. Full workbook gồm toàn catalogue active đã biết tại freeze, **không có nghĩa toàn catalogue vừa được crawl hoặc xác nhận coverage/freshness**.
- Excel bundle: `exports/domestic/<batch-id>/{full.xlsx,delta.xlsx,.bundle.json}`. Report append-only: `exports/diagnostics/retry/<batch-id>/<instance-id>/{report.json,REPORT.md,manifest.json}`. All-skipped nằm dưới `diagnostics/retry/invocations/`. Directories private `0700`, files `0600`; raw copies giữ bytes/hash/time, không nhúng vào Markdown chia sẻ. Không chia sẻ raw hoặc DB credentials.
- Chẩn đoán tách **observed failure** và **root cause: chưa xác định**. JSON có source/retry IDs, baseline/accepted/display refs, step facts, raw locators/hash và findings. Legacy source có thể thiếu observations; bare TimeoutError hay pageerror đồng thời không chứng minh lỗi mạng/JS. Retry complete chỉ chứng minh không tái hiện lần đó, không chứng minh nguyên nhân cũ là transient. Error text tùy ý không được đưa vào báo cáo chia sẻ; dùng run/error-index locators tới DB. Structured report loại userinfo/query/fragment cả trong URL tuyệt đối, URL tương đối và action/state keys; raw copies private giữ nguyên bytes.
- `retry-report` chỉ SELECT trong consistent read-only transaction và ghi instance mới, không parse/capture/apply/freeze/recover/export. Running batch được đánh dấu chưa hoàn tất (`2`); frozen report thành công trả `0`. Nếu report/export lỗi, giữ batch/run IDs đã in trước network; dùng `retry-report` hoặc `export-batch` cho **batch mới**, không chạy lại network chỉ để lấy report.

**Docker:** build tag riêng, không `compose up/restart/recreate` và không exec CLI mới vào daemon image cũ. Lượt này đã build `hanoitourist-crawler:scoped-retry-5e4ff8ee`; worker smoke dùng đúng existing project network `hanoitourist-crawler-verify_default`, UID/GID `501:20`, shm 1 GiB và absolute exports bind mount. Secrets truyền qua environment (`--env DATABASE_URL`, không giá trị DSN trong argv). Hai command help không cần DB; Chromium/timezone/non-root writable output, DB identity và hashes sáu modules đã kiểm. Image scheduler đang chạy **không được cập nhật** và lịch/TTL không đổi.

**Kết quả kiểm thử mới:** full suite PostgreSQL/Chromium/native trên DB scratch riêng: **500 passed, 0 skipped (438.27s)**. Lượt targeted tiếp theo: **373 passed / 1 failed (449.99s)** ở `test_html_native_navigation_bounds_fail_closed[meta]`. Reproduction loopback cho thấy meta-refresh tới `/booking` thực sự lọt qua HTML discovery acquisition: **baseline trước sửa 4/5 failures; implementation 5/5 failures**. Đây là safety blocker có từ trước, không phải chỉ sai request-count; không nới assertion hoặc sửa navigation/discovery ngoài scope để che lỗi. **Không chạy live hai URL, không tạo retry batch nghiệp vụ**. Cần review mở rộng scope sửa acquisition guard, rồi chạy lại gates trước nghiệm thu live.

Evidence/baseline/commands/results: `exports/diagnostics/scoped-retry-20261019T162629Z-5e4ff8ee/REPORT.md`. Giữ nguyên báo cáo/dữ liệu lịch sử e29 và các thay đổi working tree của người dùng; không stage/commit/reset hoặc cleanup scratch/volume tự động.

### Sửa meta-refresh guard và chạy hai URL — follow-up 19/10/2026

**Đã sửa blocker và đã chạy đúng hai URL bằng một invocation public Docker CLI. Kết quả thực tế: Gốm partial, Hà Giang failed/HTTP 403; không có accepted complete mới.** Các FAIL ở lượt trên vẫn là evidence lịch sử, không bị ghi đè hoặc đổi thành PASS.

- Nguyên nhân guard leak đã tái hiện: HTML discovery gọi `context.unroute_all(behavior='wait')` khi context/page còn sống. Playwright gỡ routes trước khi chờ handlers, nên native meta-refresh có thể phát request ngoài guard trong teardown. Regression trước fix ghi nhận hits thật tới booking/admin/comment. Fix hẹp trong `discovery.py`: giữ routing tới khi đóng context, rồi luôn đóng browser; lỗi cleanup không che capture failure/KeyboardInterrupt. Không sửa shared navigation/detail collector, retry/publication, parser/validator, timeout/retries/pacing/caps hoặc dependencies.
- Gate host mới: unit **256 passed / 8 skipped / 256 deselected** (không cấp scratch DSN cho unit-only); targeted **394 passed (487.87s)**; full PostgreSQL/Chromium **520 passed, 0 skipped (459.99s)** trên DB scratch `hanoitourist_meta_refresh_test_d86d3516`. Host native regression **20 lượt × 6 cases = 120 passes**; image native loopback **20 lượt × 5 cases = 100 passes**, zero forbidden hits/uncaught listener errors. Installed/source Python + SQL bytes, AST/dependencies/help/diff checks PASS.
- Image riêng **`hanoitourist-crawler:meta-refresh-d86d3516`**, ID `sha256:446d6872e353ab988212f131851356313043bacab210cef23b0cf0c22cd3bc7b`. Đã kiểm hashes tám modules gồm discovery/navigation, Chromium/timezone/non-root private output và offline `retry-report` trên scratch, không đổi rows. **Scheduler vẫn image cũ, chưa deploy fix**; container ID/image/StartedAt, lịch/TTL và scheduler slot không đổi bởi lượt này.
- Live source **`f7cbe963-1a71-43a6-b403-092a057b4c0b`** → manual batch **`a697f7ee-fa51-4e0e-8234-18b72cacee74`**, partial/frozen, export/report thành công, CLI exit **2** bình thường (không phải busy). Đúng hai selected runs, không discovery hoặc recovery/catalogue crawl; không chạy lại network hay thêm URL.
  - **Chuyện của Gốm:** run `8b8c6d9d-a4c3-478e-871c-b978183019da`, capture/parse partial. Primary HTTP 200; observed `TimeoutError` sau 30.0409s tại `schedule_row_or_empty_wait`. Accepted vẫn NULL; display là partial snapshot `c7e1c75b-48af-469e-9349-c2e792a3eff3`, 0 lịch/0 giá đã quan sát, **không chứng minh tour không có lịch**. Không có rendered DOM tại timeout; raw HTTP không có row/explicit-empty selector. Root cause **chưa xác định**, không quy lỗi mạng/JS/selector chỉ từ timeout.
  - **Hà Giang:** run `27b654e1-2d65-4712-a4cf-989ccd2750f9`, capture failed/parse pending vì **owned primary HTTP 403**, có raw ID/hash trong report. Không coi 403 là unavailable hay bằng chứng nguyên nhân cụ thể. Accepted/display vẫn snapshot complete cũ `5bba28ad-13da-4d9a-8666-a70ba3e7f292` của run `99428bb0-0530-4420-9635-5547e1862b6f`: **7 lịch/7 observations/91 price cells**, không phải kết quả lần retry này.
- Workbook `exports/domestic/a697f7ee-fa51-4e0e-8234-18b72cacee74/full.xlsx`: **69 tour catalogue tại freeze**, không phải vừa crawl lại 69 tour; `delta.xlsx` không có business changes mới. Đã kiểm 7 sheets, frozen digest/receipt/file hashes, exact catalogue/snapshot/run refs của child rows, giá nguyên văn/số tiền và timestamps của observations. Delta vẫn có sheet nhật ký attempts, không có nghĩa file hoàn toàn rỗng.
- Audit giữ toàn bộ **2.889 old raw documents**, 254 terminal runs, 231 snapshots/children, source batch/rows/receipts và bundles cũ; +2 runs/+4 raw/+1 partial snapshot. Chỉ hai selected catalogue entities đổi attempt/refresh state; accepted refs giữ nguyên, không đổi unrelated tours hoặc scheduler slot trong audit interval. External audit không chứng nhận toàn DB/freshness ngoài interval.

Report sản phẩm private: `exports/diagnostics/retry/a697f7ee-fa51-4e0e-8234-18b72cacee74/08a68bf3-2638-4f11-982f-3d9440e17eba/REPORT.md`. Evidence mới, baseline/diff/hashes/commands/limitations: `exports/diagnostics/meta-refresh-repair-20261019T172619Z-d86d3516/REPORT.md`. Giữ scratch DB/evidence và dirty tree; không tự stage/commit/reset/cleanup.

### Attempt completion module — architecture refactor, 20/10/2026

`attempts.py` now owns post-capture completion ordering and rejection bookkeeping. See `GLOSSARY.md` for the domain terms. The three explicit paths preserve their existing behavior:

- `complete_daily`: parse committed staging, then publish the attempt in a separate commit; a publication failure does not roll back the parsed snapshot.
- `complete_retry`: parse/persist and selected-identity publication share one transaction; database errors, unexpected publication failures, and interruption roll back the snapshot, not the already committed capture. A recorded identity rejection can retain a complete snapshot without accepting it into the catalogue.
- `complete_recovery`: terminalize abandoned captures and publish only committed evidence; never acquire or parse. A complete capture without a snapshot remains a failed attempt, not an implicit replay.

`pipeline.collect_run` performs acquisition only. Daily crawl and scoped retry call their completion path after acquisition; legacy `capture_run` still acquires and parses without catalogue publication. The arbitrary `finalize_attempt` callback and retry's `_finalizer` closure have been removed. Selection, budgets, locks, batch freezing, exports, reports, scheduler behavior, and Storage's identity binding/alias/fingerprint/accepted-baseline rules are unchanged. No schema, configuration, dependency, or version change.

Tests now cross the completion module's interface instead of supplying arbitrary pipeline finalizers. They cover independent-reader visibility for both commit policies, publication failure/interrupt/database rollback, parse diagnostics, scoped alias rejection, recovery without parsing, and unchanged legacy behavior. Existing daily/retry/recovery and export scenarios remain in the suite.

Verification used the normally installed package (offline wheel build, no editable import), with source/installed bytes checked for all four changed production modules. Full suite on a newly created standalone scratch PostgreSQL container: **539 passed, 0 skipped (694.15s)**, including Chromium mocks and native loopback HTTP tests. The first targeted run had **115 passed / 1 failed / 13 deselected**: its recovery expectation was corrected to match existing behavior for a complete capture with no snapshot; production semantics were not changed to satisfy that assertion. The initial non-isolated package build lacked setuptools; an offline isolated build using the declared build requirements succeeded without dependency changes.

No website crawl, business-database connection, historical repair, deployment, or scheduler operation was performed. The scratch container is disposable and independent of existing Compose projects. These results verify the refactor, not live acquisition, freshness, or deployment readiness.

### Stopped scoped retry completion — offline follow-up

The existing refactor above is the flow baseline. This follow-up moves only the remaining per-attempt stop bookkeeping from `retry_domestic` into `attempts.complete_retry_stopped`. The caller still selects pending rows from its own invocation, freezes/exports/reports the batch, and propagates interruption or fatal failure. A running capture becomes failed using the existing clock/message; a terminal capture retains its status, finish time and diagnostics. The attempt is marked failed and requires refresh, without parsing, publication or replacement of accepted data. The writes retain their existing separate commits; this is not a new atomicity or idempotency guarantee.

New characterization tests cover stopped captures with and without accepted baselines, committed-but-unaccepted snapshots, conditional clock behavior, partial cleanup failure, and preservation of a previously completed selected attempt. Independent readers check daily versus retry commit visibility. Real PostgreSQL division-by-zero faults verify snapshot/publication rollback with diagnostics enabled and disabled. Recovery tests forbid acquisition/parsing for empty, raw-only and committed-snapshot cases.

Fresh verification on the normally installed package and a standalone scratch PostgreSQL database: relevant suite **456 passed, 0 skipped (527.85s)**; full offline suite **582 passed, 0 skipped (646.38s)**, including actual Chromium/native-loopback tests. All **26 Python/SQL package files** match source; AST checks, dependency compatibility, CLI help outside the repo and `git diff --check` pass. The new helper's tracer initially failed because the planned interface was absent, then passed after the move; this is not a reproduced production data-corruption fix. No schema, dependency, version, domain-model, live acquisition, business-database, deployment or scheduler changes.

### Tổ chức lại thư mục — kiểm chứng offline

Source và tests đã được nhóm theo `workflows/`, `acquisition/`, `processing/`, `persistence/`, `reporting/`; xem bản đồ/thứ tự đọc ở đầu README. Chỉ đổi import paths và SQL resource location/packaging, không đổi logic crawler. CLI giữ nguyên; import nội bộ phẳng cũ không có shim chuyển tiếp.

- Baseline mới trước khi chuyển: **582 passed, 0 skipped (650.23s)**. Full suite sau chuyển và clean wheel install: **590 passed, 0 skipped (613.26s)**, gồm Chromium mocks, native HTTP loopback và PostgreSQL schemas UUID trên scratch riêng. Collection đối chiếu đủ 582 ca cũ; 8 ca mới gồm 5 group-layout tests, 1 SQL-resource test và 2 installed CLI entry-point cases.
- Tracer của từng nhóm được chạy đỏ vì đường dẫn mới chưa tồn tại rồi xanh sau move/reinstall; đây là interface-absence red, không phải tái hiện lỗi dữ liệu. Assertions nghiệp vụ và fixture bytes cũ giữ nguyên.
- Exact file sets/bytes của **29 Python + 2 SQL files** khớp source, wheel và normal installed package; không còn flat modules/root SQL cũ. Hai SQL hashes giữ nguyên. Ngoài repo, cả console/module entry points đều chạy được 10 dạng help không cần database env; `db init` qua hai entry points tạo đúng 13 bảng trên scratch và lần thứ hai giữ schema/rows nguyên vẹn.
- AST kiểm 58 source/test/marker files; đối chiếu với flow baseline xác nhận production behavior và existing test assertions không đổi ngoài import/resource rewiring. Versions/dependencies/config/fixtures/GLOSSARY, các báo cáo lịch sử trong README và **2.128 export/evidence files** được giữ nguyên. `uv pip check` (23 packages) và `git diff --check` đạt.
- Có một invocation acquisition+processing bị external tool timeout sau 1800s, không có per-test log/exit từ runner buffered ban đầu nên **không tính là pass**. Sau khi bổ sung progress logging/bounded wait ở private verification runner, cùng selection với verbose/faulthandler diagnostics đạt **295 passed (203.46s)**; full suite cuối ở trên cũng đạt. Chưa xác định nguyên nhân lượt timeout; không sửa production code hoặc nới crawler budgets/assertions để né nó.

Không crawl live, truy cập DB nghiệp vụ, bật scheduler, deploy, migration/backfill hay thay domain model trong đợt sắp xếp này. Kết quả offline không chứng nhận live coverage/freshness hoặc deployment readiness.
