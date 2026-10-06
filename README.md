# Hanoitourist — crawler thử nghiệm một tour

Python CLI chạy thủ công cho **duy nhất** URL:

https://hanoitourist.vn/kham-pha-phu-quoc-hon-ngoc-vien-dong

Thu thập nội dung tour, mọi accordion lịch trình, ảnh dạng URL và tất cả lịch khởi hành/giá chi tiết. Lưu raw staging và dữ liệu quan hệ trong PostgreSQL, giữ lịch sử từng lần chạy, xuất JSON để đối chiếu và Excel để xem trực quan. Chưa có crawl danh sách, UI hay scheduler.

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

## Chạy thủ công

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

Package hiện tại là **`0.1.5`** (thêm export Excel với `openpyxl`); parser/collector vẫn **`0.1.4`** vì logic trích xuất không đổi. Replay run cũ tạo snapshot `0.1.4` cạnh các version cũ, không sửa chúng và không cần migration schema. `run.errors` giữ lịch sử: capture errors legacy là strings; parse failures mới là records có `stage`, `parser_version`, `error_type`, `occurred_at`, `message` an toàn. Parser nhận diện cả format lỗi parse legacy. Lỗi parse lịch sử không làm snapshot mới thành `partial` nếu capture đầy đủ và lần parse hiện tại thành công; lỗi capture/không phân loại được vẫn được xét bảo thủ.

## Raw, dữ liệu động và ngữ nghĩa giá

- **HTML gốc**: response của chính navigation browser. **DOM**: HTML sau JavaScript và sau các tương tác. **AJAX**: response lịch (Drupal commands `openDialog`/`insert` chứa HTML).
- Raw `BYTEA` giữ response body đã giải nén hoặc DOM được serialize, kèm SHA-256/URL/state/time; không phải wire packet archive. HTTP/DOM toàn trang có thể chứa menu/footer, nhưng parser không đưa chúng, quảng cáo, chat hay tour gợi ý vào dữ liệu tour.
- Raw được commit theo checkpoint **trước** parser. Parser chỉ đọc staging. Không tải ảnh/video/JS/CSS để archive; browser có thể tải resource cần render. Ảnh chính, thumbnail, URL lazy-load và icon phương tiện được phân loại; carousel clones không nhân số ảnh.
- Ngày/giá/số chỗ giữ cả chuỗi hiển thị và giá trị chuẩn hóa. Tiền là Decimal/NUMERIC, JSON amount là chuỗi chính xác, không float. `0đ` vẫn là zero của nguồn, **không suy diễn miễn phí**. Rỗng/missing/parse error không bị đổi thành zero.
- Giá quảng bá sidebar không thay thế giá của từng lịch. Ma trận giá giữ nhãn nhóm tuổi, phụ thu, giảm giá, rowspan/colspan. Ô phụ thu phòng đơn gộp nhiều cột lưu một ô chung; không nhân thành nhiều khoản thu và không tự tính giá thanh toán.
- Giá option trong lịch trình và các vé tham quan không bao gồm vẫn nằm đầy đủ trong text/HTML section, không nhầm thành giá khởi hành.
- Nếu cùng lịch khác giá/số chỗ giữa page/modal/checkpoints, giữ observations riêng, ghi warning; không chọn giá thấp nhất hoặc dùng dữ liệu lần trước để bù.
- Crawl là một khoảng thời gian: run có start/end, mỗi raw/observation có thời điểm riêng. Times được DB lưu `TIMESTAMPTZ`, export ISO 8601. Giờ khởi hành không ghi timezone trên site được diễn giải theo `Asia/Ho_Chi_Minh`, không suy luận đó là giờ chuyến bay.

## Schema và truy vấn

Tám bảng: `crawl_runs`, `raw_documents`, `tour_snapshots`, `tour_sections`, `itinerary_days`, `tour_media`, `departure_observations`, `price_cells`. Snapshot/child rows được ghi atomic; raw commits độc lập. Metadata/provenance/coverage dùng JSONB; ngày/giá/ô bảng có bảng và cột quan hệ, không chỉ JSON blob.

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

- Kiểm tra robots từng đường dẫn dữ liệu; robots lỗi/unrecognized thì fail closed. Không bypass 401/403/CAPTCHA. 429 tôn trọng Retry-After trong time budget.
- Context mới mỗi run, không dùng phiên đăng nhập. Chặn booking, comment/tư vấn submit, statistics/analytics/chat. Chỉ whitelist AJAX POST đã xác minh chỉ đọc của đúng lịch tour, không cho POST tùy ý.
- Browser mở mọi accordion và chi tiết giá của từng lịch, mở modal lịch; pager/dropdown hữu hạn được duyệt trong scope đúng tour, không dùng pager footer. Điều khiển không hiểu/loop/hết giới hạn báo partial.
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
