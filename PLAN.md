# Incremental loading — tour trong nước Hanoitourist

## Phạm vi đã chốt

- Quét danh mục `https://hanoitourist.vn/tour-trong-nuoc` và toàn bộ phân trang thuộc danh mục đó; không crawl tour nước ngoài, không dò toàn website.
- Tận dụng dữ liệu đã crawl trong PostgreSQL. Tour mới lấy ngay; tour cũ kiểm tra lại sau **7 ngày tính từ lần capture + parse đầy đủ thành công gần nhất**.
- Mỗi tour được chọn vẫn lấy đầy đủ nội dung, ngày lịch trình, ảnh/link, lịch khởi hành và ma trận giá như crawler hiện tại. Không dùng dữ liệu card danh mục thay cho dữ liệu chi tiết.
- Đánh dấu tour đã crawl là không khả dụng khi biến mất khỏi một lần discovery đầy đủ thành công hoặc navigation trang chi tiết trả 404/410. Không xóa lịch sử. Timeout, 401/403, 429, 5xx, CAPTCHA, lỗi robots/parse không phải bằng chứng ngừng hoạt động.
- Tour không khả dụng xuất hiện lại được crawl ngay, không chờ đủ 7 ngày; chỉ khôi phục khả dụng sau khi capture và parse đầy đủ thành công.
- Sau mỗi batch có hai workbook XLSX: **delta** (tour mới, đổi dữ liệu, đổi trạng thái) và **full** (toàn bộ tour trong scope đã biết, kể cả không khả dụng).
- Chạy tự động trên máy cá nhân bằng Docker Compose, mặc định **00:00 mỗi ngày, Asia/Ho_Chi_Minh**; cho phép đổi lịch bằng cấu hình. Bỏ lỡ nhiều lịch thì chạy bù **một** lượt cho mốc đến hạn gần nhất khi service hoạt động lại.
- Không chạy chồng các lượt crawl. Không có UI, booking, login/payment, submit form, dịch vụ cloud hoặc yêu cầu thuê VM.
- **Yêu cầu bổ sung:** trước mọi thay đổi implementation, khởi tạo Git local và commit trạng thái source/config/tests/docs hiện tại để có baseline đối chiếu/khôi phục. Không push remote hoặc commit secrets/dữ liệu runtime.

Đây là incremental ưu tiên **giảm request**: một lần kiểm tra đến hạn vẫn tạo run/raw/snapshot lịch sử riêng như hiện tại, kể cả nội dung không đổi; chỉ dữ liệu mới/thay đổi mới vào delta. Không thêm dedup/xóa raw hoặc thay hợp đồng lưu lịch sử để tiết kiệm DB.

## Căn cứ khảo sát và giới hạn của ARCHITECT

Đã đọc đầy đủ README, pyproject, Compose/env, các module config/models/storage/collector/parser/selectors/validation/pipeline/CLI/Excel/JSON/errors/normalize và các tests liên quan. CodeGraph `status`, `explore`, `query`, `node`, `callers`, `impact`, `affected` xác nhận:

- `Config.validate()` hiện chỉ chấp nhận đúng URL Phú Quốc; không thể đơn giản loop qua URL danh mục bằng `Config.load()` hiện tại.
- `crawl -> Collector.collect -> parse_run -> parse -> validate -> Storage.persist_snapshot` là pipeline đang dùng. Raw commit trước parse; snapshot/children atomic; replay idempotent theo `(run_id, parser_version)`.
- `Collector.data_path/route` chỉ cho trang tour hiện tại và AJAX lịch của đúng node. Không được nới whitelist thành toàn bộ `hanoitourist.vn` để thêm discovery.
- `Collector.collect` staging được response lỗi nhưng chưa có hợp đồng navigation rõ ràng để phân biệt 404 của trang tour với 404 của AJAX/resource.
- `Storage.init_schema()` hiện chỉ chạy `sql/001_initial.sql`; chưa có catalogue, batch, watermark hay scheduler state.
- `export_excel/_workbook` chỉ có một snapshot. Các tests Excel/CLI/export/storage/pipeline chịu tác động nếu thay interfaces cũ; phải giữ entrypoint và bố cục export một tour.
- PostgreSQL, Playwright, BeautifulSoup, httpx và openpyxl đã có. Scheduler có thể dùng standard library, không cần APScheduler/Redis/Celery.
- Package `0.1.5`, parser/collector `0.1.4`; README ghi 143 tests của lần nghiệm thu trước, **không phải** kết quả chạy trong bước ARCHITECT này.

Repo chưa có `.git`, không có fixture danh mục trong nước, và chưa khảo sát live DOM/pager/robots của URL danh mục trong bước này. Không giả định selector listing hoặc whitelist AJAX từ markup của tour Phú Quốc. Bước 2 dưới đây là prerequisite kiểm chứng adapter khi IMPLEMENT, không phải yêu cầu đổi phạm vi hay phỏng đoán rằng discovery đã hỗ trợ site.

ARCHITECT chỉ thay `PLAN.md`; không tạo Git/commit, không chạy tests/crawl, không migrate DB, không build/install hay sửa source/config/fixtures/exports. Git baseline được tạo **đầu IMPLEMENT**, trước regression tests hoặc source edits.

## Hợp đồng dữ liệu và vận hành

### A. Catalogue và watermark

- Tách **khả dụng** (`pending`, `available`, `unavailable`) khỏi **chất lượng snapshot** (`complete`, `partial`) và khỏi việc còn được **niêm yết trong danh mục**. Có card không đủ để xác nhận tour đã lấy đủ dữ liệu.
- `current_snapshot_id` chỉ trỏ tới snapshot đã capture/parse `complete`. `latest_attempt_run_id` giữ attempt mới nhất, kể cả lỗi. Không ghi đè dữ liệu đầy đủ bằng partial, không chắp dữ liệu cũ vào dữ liệu partial để báo đủ.
- Với tour mới chưa có complete snapshot, full vẫn có dòng thông tin định danh/card và trạng thái `pending`, đánh dấu rõ chưa đủ dữ liệu. Nếu có partial snapshot thì xuất phần đã quan sát được với nhãn partial; không giả lập dữ liệu bị thiếu. Tour mới chỉ có sự kiện `new` sau lần complete đầu tiên; lỗi/partial nằm trong nhật ký.
- Kiểm tra thành công, dù hash không đổi, cập nhật `last_successful_check_at` bằng `crawl_runs.finished_at`; **không** dùng `parsed_at`, thời điểm replay, export hoặc discovery để kéo dài 7 ngày.
- Lựa chọn crawl: chưa có complete baseline; `requires_refresh`; tour unavailable nhưng hiện được liệt kê; hoặc `now >= last_successful_check_at + recheck_days`. Failed/partial không tiến watermark, được thử lại ở batch tiếp theo nếu còn trong listing. Tour unavailable không còn listed không bị tải chi tiết mỗi ngày.
- HTTP 404/410 chỉ được kết luận từ response navigation chính đã staging của tour được chọn, không từ ảnh, robots, AJAX lịch hay exception string. Lưu reason, thời điểm và run/raw evidence; không tiến watermark thành công.
- Absence reconciliation chạy **sau** discovery đầy đủ và xử lý identity/alias. Chỉ đánh dấu các tour đã crawl thuộc registry trong nước; không đánh dấu mất tour từ toàn bộ `tour_snapshots` không liên quan.
- Không suy ra unavailable từ hết lịch, không còn chỗ hoặc giá “Liên hệ”. Nếu vẫn là tour hợp lệ, xử lý bằng coverage hiện tại; trường hợp chưa được adapter hỗ trợ phải partial.

### B. Tận dụng lịch sử và identity

- Seed baseline từ dữ liệu hiện có cho URL tour Phú Quốc đang được repo hỗ trợ và cho các URL/identity được discovery xác nhận thuộc danh mục trong nước. Không tự nhập tất cả snapshot của host vào scope này.
- Chọn complete snapshot của **lần capture thành công mới nhất**, rồi snapshot parse mới nhất của chính run đó, tie-break bằng ID. Run gần đây partial/failed không làm mất complete baseline cũ. Một replay hôm nay của capture cũ không được coi là lần kiểm tra hôm nay.
- Nếu lịch sử chỉ có partial thì giữ để xem, nhưng tour vẫn cần lấy lại ngay. Nếu thiếu `finished_at` đáng tin cậy, giữ nội dung baseline nhưng để watermark chưa xác định và kiểm tra ngay.
- Seed idempotent khi một entity được đưa vào catalogue lần đầu, không phát sinh delta `new` cho dữ liệu cũ và không thay raw/snapshot lịch sử. Không chạy lại bootstrap theo cách reset watermark/state ở mỗi batch.
- URL discovery là key ban đầu; `source_tour_id` xác minh từ chi tiết là identity ổn định. Lưu alias URL để canonical/trailing slash/slug redirect không tạo tour trùng. Source ID thay đổi trên URL đã có không được âm thầm ghi đè identity cũ: ghi lỗi/partial để xem lại.
- Khi URL mới xác minh được source ID của tour đã có, transaction chuyển alias về entity cũ và hợp nhất candidate pending; phân loại theo baseline entity cũ, không báo tour mới giả. Giữ entity pending đã có lịch sử bằng `superseded_by_id`, không xóa dữ liệu lịch sử.

### C. Hash và delta

- Tạo fingerprint có version riêng (`v1`) trên projection nội dung nghiệp vụ, không hash raw HTML toàn trang hay serialized snapshot nguyên xi.
- Bao gồm title, URL nguồn chuẩn, source ID, duration/origin, giá/chỗ raw và normalized, review/sidebar, mọi section/day variant (text + signature HTML), media/link, mọi departure observation và price cell (kể cả nhãn/rowspan/colspan/null/zero).
- Loại UUID run/snapshot/children/raw, provenance, thời điểm capture/parse/observed, state key giao diện, coverage, warning và parser/collector version. Canonical-sort collections; loại bản sao observation chỉ khác metadata kỹ thuật nhưng giữ các giá trị nghiệp vụ xung đột.
- Tái sử dụng nguyên tắc `_content_signature` của parser để bỏ đúng trạng thái accordion/lazy-image đã được xác minh. Không strip mọi `data-*`, mọi URL query/fragment, HTML hoặc nội dung chỉ để làm hash ổn định. Nếu phát hiện markup kỹ thuật mới gây hash nhiễu, chỉ whitelist sau khi có fixture regression.
- Delta là dữ liệu đầy đủ mới nhất của mỗi tour thay đổi, không phải diff từng ô. `change_kinds` có thể gồm `new`, `updated`, `unavailable`, `reactivated`; một tour chỉ xuất một lần trong delta của batch.
- Dữ liệu cũ unchanged/seed/skipped không vào delta. Một lần lỗi làm thay warning/attempt metadata cũng không phải cập nhật dữ liệu tour. Đổi availability phải vào delta dù nội dung không đổi hoặc chỉ còn snapshot cũ.

### D. Batch, export và scheduler

- Batch giữ liên kết discovery run + từng detail run, outcome/counters/errors và snapshot refs trước/sau. Freeze selection full/delta ở cuối xử lý trong DB; export lại batch cũ **không** query “latest catalogue hiện tại”.
- Hai workbook batch dùng sáu sheet nghiệp vụ hiện có và thêm sheet **Nhật ký tải** cho status/errors/counts. Entry point export một tour giữ nguyên sáu sheet.
- Full bao gồm skipped/cached/unavailable với dữ liệu cũ và thời điểm kiểm tra thật. Delta rỗng vẫn có workbook có headers và batch status; discovery lỗi không được trình bày như “không có thay đổi, thành công”.
- Một PostgreSQL **session advisory lock** cho các lệnh có mạng (`crawl`, `crawl-domestic`, lịch tự động), cùng namespace DB/schema. Lock sống qua raw commits nhưng không giữ SQL transaction trong thời gian browser chạy. Parse/export offline không cần lock này.
- Scheduler tick mặc định 60 giây; tính mốc lịch đến hạn bằng giờ địa phương có ZoneInfo, lưu mốc UTC trong DB. Sau khi lấy lock phải kiểm tra lại scheduler state trước khi chạy. Một batch terminal (complete/partial/failed) tính là đã xử lý mốc, tránh retry nóng cùng ngày; failed tours vẫn due vào lượt sau. Busy không consume mốc.
- First start chưa có scheduler state: chạy một lượt cho mốc gần nhất đã đến hạn. Khi bỏ lỡ nhiều ngày: không enumerate/crawl từng ngày, chạy một batch cho mốc gần nhất và tiến `last_handled_slot` tới mốc đó.
- Máy ngủ trong lúc đang crawl: khi thức dậy vẫn kiểm tra deadline/lock trước khi tiếp tục. Docker/service bị dừng hẳn: lock được DB giải phóng; lần tiếp theo dưới lock phục hồi batch `running` bỏ dở thành partial, freeze/export các thay đổi đã commit và giữ raw/attempt history. Với scheduled batch cũ đã xử lý một phần, ghi nhận mốc cũ terminal; chỉ chạy catch-up mới nếu có mốc mới hơn đến hạn. Tour chưa xử lý không bị tiến watermark.
- Export failure không rollback catalogue/snapshot và không crawl lại chỉ để sửa file; batch có export status riêng và lệnh export offline để khôi phục. Không tự thực hiện migration khi mỗi tick/crawl chạy.

## Các bước triển khai có thứ tự

### 1. Khởi tạo Git và commit baseline trước mọi implementation

**Files:** `.git/` **mới** (metadata Git local, gồm `.git/info/exclude`); stage các file hiện tại `PLAN.md`, `README.md`, `pyproject.toml`, `compose.yaml`, `.env.example`, `.gitignore`, `src/hanoitourist_crawler/` và `tests/`. Không sửa source/config/tests trước commit này.

**Thay đổi:** kiểm tra lại repo chưa có Git, rồi `git init` tại root dự án. Nếu repository đã được tạo trong lúc chờ IMPLEMENT thì không re-init/reset/overwrite; kiểm tra trạng thái và tạo baseline trên repository đó theo các thay đổi hiện có. Kiểm tra Git author identity bằng cấu hình có sẵn; nếu thiếu thì hỏi người dùng name/email, không tự bịa identity hoặc sửa global config. Exclude `.codegraph/` generated bằng `.git/info/exclude` local; `.gitignore` hiện có tiếp tục loại `.env`, `.venv`, exports/build/caches/egg-info.

Stage bằng danh sách đường dẫn rõ ràng, **không `git add .` mù quáng**. Review staged paths và diff để chắc không có secrets/artifacts ngoài ý muốn. Commit message đề xuất: `chore: snapshot existing crawler before incremental loading`. Ghi lại baseline commit SHA; không add remote/push, không amend history, không dùng reset/clean để làm working tree sạch. Sau commit mới bắt đầu thêm regressions/source changes ở các bước 2–11.

**Kiểm tra:** `git status`, staged file list/diff, commit thành công và `git show --stat`/SHA cho baseline. Không cần chạy tests hoặc reinstall trước snapshot. Baseline phải chứa phiên bản crawler hiện tại cùng PLAN cuối cùng này, không chứa implementation mới.

**Vì sao:** người dùng yêu cầu lưu trạng thái hiện tại trước khi triển khai để review diff và có điểm quay lại.

**Rủi ro:** commit `.env`/exports chứa dữ liệu hoặc thiếu Git identity; repository xuất hiện trong lúc chờ; hiểu nhầm Git là backup DB. Git chỉ lưu mã/config mẫu/tests/docs, **không backup PostgreSQL hay runtime exports** và không thay/chụp dữ liệu DB trong bước này.

### 2. Xác minh adapter listing và khóa regression scope

**Files:**
- `src/hanoitourist_crawler/selectors.py`
- `tests/fixtures/domestic_listing_page_1.html` **mới**
- `tests/fixtures/domestic_listing_page_2.html` **mới**
- `tests/fixtures/dynamic_domestic_listing.html` **mới**
- `tests/test_discovery.py` **mới**
- `tests/test_parser.py`

**Thay đổi:** sau Git baseline, khảo sát read-only robots, main listing, card/title link, empty-result, pager cuối và request của danh mục. Ghi selector/view identity đã xác minh; chỉ fixture mới được thêm, không sửa fixture tour lịch sử. Tạo fixtures tối giản phản ánh markup đã xác minh, kèm dynamic mock để kiểm tra render/pagination. Không crawl toàn bộ chi tiết chỉ để khảo sát danh mục. Nếu site chặn truy cập hoặc không chứng minh được pagination terminal thì adapter fail closed/partial và báo phần live chưa kiểm chứng.

**Tests:** 2 trang có URL trùng; header/menu/footer/tour gợi ý/tour nước ngoài không nhập inventory; card thiếu hoặc sai href vẫn được ghi nhận unresolved thay vì biến mất; pager/total count không khớp gây partial; tour thứ hai có node ID/URL/lịch khác vẫn dùng parser/validator chung và giữ toàn bộ giá/nội dung, không hardcode ID 8140.

**Vì sao:** coverage danh mục là điều kiện an toàn cho việc đánh dấu unavailable; repo chưa có bằng chứng về markup này.

**Rủi ro:** HTTP 200 nhưng là CAPTCHA, listing hydrate muộn, nhiều pager ngoài scope, hoặc AJAX khác lịch tour. Không dùng CSS guessed để tuyên bố complete; không tự whitelist `view_args=137` từ test cũ.

### 3. Thêm contracts, migration additive và repository catalogue

**Files:**
- `src/hanoitourist_crawler/models.py`
- `src/hanoitourist_crawler/storage.py`
- `src/hanoitourist_crawler/sql/002_incremental.sql` **mới**
- `tests/test_storage.py`
- `tests/test_catalogue_storage.py` **mới**
- `tests/conftest.py`

**Thay đổi:** thêm dataclasses nhỏ cho discovered tour/result, tour state và batch result; giữ models snapshot/raw hiện tại. Migration tạo năm bảng:

1. `domestic_batches`: ID, origin/manual-scheduled, scheduled_for, discovery_run_id, started/finished, status, discovery_complete, counts/errors JSONB, stage, export_status/export_manifest/export_errors. Không ghi lỗi chứa credentials.
2. `domestic_tours`: ID, source_tour_id nullable, canonical_url, listing_title, is_listed, availability/reason/changed_at, current_snapshot_id, latest_attempt_run_id, last_attempt_at, last_successful_check_at, last_seen_batch_id, requires_refresh, fingerprint/version, superseded_by_id nullable. Unique source ID cho entity chưa superseded; indexes phục vụ due/availability.
3. `domestic_tour_urls`: normalized URL key duy nhất, tour_id FK, first_seen_at, last_seen_batch_id; alias chỉ bind sau bằng chứng discovery/chi tiết hợp lệ.
4. `domestic_batch_tours`: `(batch_id,tour_id)` PK, discovery/selection reason, attempt run refs, outcome/errors, previous/result/display snapshot refs, previous/result availability, hashes, change_kinds. Tạo row cho cả tour skipped/unavailable khi freeze để tái xuất full đúng lịch sử. Hợp nhất alias không làm mất các attempt refs.
5. `scheduler_state`: job_key PK, last_handled_slot UTC và updated_at; không suy scheduler watermark từ file hoặc thời gian parse.

`init_schema` chạy các script packaged theo thứ tự `001`, `002` trong transaction idempotent, không sửa `001` hoặc bảng lịch sử. Preflight incremental kiểm tra bảng mới và báo cần `db init`; preflight legacy giữ được chức năng cũ. Thêm repository methods cho seed/query latest history by capture time, upsert aliases, due selection, apply outcome/event atomic, reconcile absence, freeze/get batch, recover abandoned batches, schedule slot và session lock. Snapshot children vẫn do `persist_snapshot` hiện có quản lý; thêm lookup theo snapshot ID để đọc refs cố định, không thay semantics `get_snapshot(run_id, version)`.

**Tests:** apply migration vào schema chỉ có 001 và dữ liệu cũ; init lặp lại; raw/snapshot IDs/body/hash/time không đổi; JSONB/null/Decimal/timestamps round-trip; bootstrap capture-vs-replay ordering; alias/source-ID uniqueness; rollback chỉ state update, không mất staging; hai connections cùng schema tranh lock, unlock sau close; freeze refs vẫn giữ nguyên khi catalogue cập nhật ở batch sau.

**Vì sao:** cần persistent watermark, catalogue và audit batch thay vì suy trạng thái từ files export hoặc một JSON blob toàn bộ ứng dụng.

**Rủi ro:** migrations/foreign keys tạo vòng phụ thuộc, session lock reentrant không cân bằng, alias merge làm mất delta hoặc reset watermark. Tạo batches trước tours/aliases/results, dùng SQL parameterized và identifiers an toàn; giữ snapshot history append-only.

### 4. Cấu hình scope, lịch, budget và URL safety

**Files:**
- `src/hanoitourist_crawler/config.py`
- `src/hanoitourist_crawler/scope.py` **mới**
- `.env.example`
- `tests/test_config.py`
- `tests/test_scope.py` **mới**

**Thay đổi:** giữ URL default và lệnh crawl một tour cũ. Thêm fields/env:

- `DOMESTIC_LISTING_URL=https://hanoitourist.vn/tour-trong-nuoc` — scope root cố định, không thành arbitrary website crawler.
- `RECHECK_DAYS=7`.
- `DISCOVERY_TIMEOUT=300`, `MAX_LISTING_PAGES=50` — riêng với giới hạn lịch khởi hành hiện tại.
- `BATCH_TIMEOUT=21600` — budget mặc định 6 giờ, cho phép tăng; hết budget đánh dấu phần chưa làm là pending/partial, không truncate rồi báo complete.
- `SCHEDULE_TIME=00:00`, `SCHEDULE_TIMEZONE=Asia/Ho_Chi_Minh`, `SCHEDULER_POLL_INTERVAL=60`.
- `SCHEDULER_DATABASE_URL` dành cho Compose nội bộ, tách `DATABASE_URL` host đang dùng localhost.

Giữ CLI > env > default. Validate finite/positive budgets, recheck days, định dạng HH:MM, ZoneInfo và user-agent. Manual config vẫn giữ scope tour cũ; batch chỉ tạo config per-tour bằng `dataclasses.replace` sau khi discovery/scope validator chấp nhận link, không nới `TOUR_URL` thành mọi URL cùng host.

Scope helper chuẩn hóa HTTPS đúng hostname (không userinfo/port lạ), relative links và trailing slash; bỏ fragment/tracking params đã whitelist của card URL, không bỏ query nội dung tùy ý. Link lạ/unrecognized ghi unresolved. Pager chỉ nhận path/query thuộc listing/view đã xác minh, không theo category menu tùy ý hoặc booking/admin/user/comment. Query/fragment của links trong nội dung tour vẫn được giữ như hiện tại.

**Tests:** overrides/defaults; invalid HH:MM/timezone/NaN/0; host giả, scheme-relative external, credentials trong URL, booking và pager ngoài scope; canonical/trailing slash/card tracking; unknown query không được âm thầm merge; detail URL hợp lệ từ listing tạo config được dùng mà manual scope cũ vẫn nguyên.

**Vì sao:** phân tách budget từng tour với toàn batch và giới hạn authority của discovery, đồng thời cho người dùng đổi giờ chạy sau này.

**Rủi ro:** nới URL validator quá rộng, hostname suffix check, timezone thiếu trên image hoặc budget 300 giây vô tình áp cho tất cả tour. Không log DSN; không hardcode lịch vào source.

### 5. Triển khai discovery có raw staging và completeness độc lập

**Files:**
- `src/hanoitourist_crawler/discovery.py` **mới**
- `src/hanoitourist_crawler/selectors.py`
- `src/hanoitourist_crawler/collector.py`
- `tests/test_discovery.py`
- `tests/test_collector.py`

**Thay đổi:** `discover_domestic(config, storage, discovery_run_id, ...)` dùng browser read-only, checkpoint source/DOM và AJAX nếu adapter đã xác minh. Dùng `crawl_runs` + `RawDocument` hiện có với role `listing`/`listing_state`; listing manifest riêng, **không** gọi tour parser cho discovery run. Stage raw trước parse listing/inventory; state/sequence/body/budget không bị truncate.

Enumerate card rows trong main listing trước khi lọc identity, ghi URL/title/source ID nullable/provenance và unresolved reason. Traverse chỉ pager của scope; lưu visited URL/state, page inventory, next/terminal evidence và total count nếu site cung cấp. Complete chỉ khi navigation/render/cards đều hợp lệ, không thiếu unresolved cards, pager terminal được xác minh và không còn actions pending. Empty danh mục phải có explicit marker trong đúng scope. Partial discovery có thể cung cấp positive links hợp lệ để xử lý tiếp, nhưng tuyệt đối không authorize absence reconciliation.

Giữ robots fail-closed, rate limit, finite retries/Retry-After và không bypass access gates. Network policy listing độc lập và hẹp: chỉ listing/pager và đúng read-only AJAX đã xác minh; không dùng node-ID lịch tour làm whitelist AJAX danh mục. Tạo `RequestPacer` nhỏ trong collector để chia sẻ khoảng cách request dữ liệu giữa discovery và các detail collector trong cùng batch; mỗi browser vẫn tuần tự, không thêm concurrency/browser pool. Không làm lại toàn bộ network stack của collector.

**Tests:** HTTP/DOM dynamic listing, hai pages/duplicates/terminal, repeated pager/state, cap/timeout/raw limit, source-vs-DOM coverage, explicit zero vs selector missing; robots unavailable/disallow; 403/CAPTCHA/429/5xx; unsafe POST và AJAX sai view_args; external/menu/booking không được navigate; valid links còn dùng được khi một trang sau lỗi nhưng result partial; pacer không reset giữa tour.

**Vì sao:** không có discovery đầy đủ thì không thể kết luận tour đã bị gỡ. Dữ liệu listing cũng cần bằng chứng raw để chẩn đoán lỗi adapter.

**Rủi ro:** discovery và parser cùng bỏ card, HTML-only fallback làm false complete, hoặc reuse whitelist làm mở POST tùy ý. Unrecognized controls fail conservatively; không click search/form/filter chưa xác minh.

### 6. Phát hiện navigation outcome và fingerprint nội dung

**Files:**
- `src/hanoitourist_crawler/collector.py`
- `src/hanoitourist_crawler/fingerprint.py` **mới**
- `tests/test_collector.py`
- `tests/test_fingerprint.py` **mới**
- `tests/test_parser.py`

**Thay đổi:** collector ghi navigation attempts/final URL/status/raw ID vào manifest để batch phân loại lỗi bằng dữ liệu có cấu trúc. Hỗ trợ redirect chi tiết có giới hạn từ URL đã chọn tới URL tour HTTPS cùng host, chặn đường booking/admin/user/category bất hợp lệ và kiểm tra source identity sau parse. Không cho redirect tùy ý trở thành link data được phép. Resource/AJAX lỗi không thay terminal navigation outcome; staging 404/410 không gọi parse tour như có dữ liệu.

`fingerprint(snapshot)` tạo projection/version/hash theo hợp đồng C, dùng Decimal/timestamp canonical chính xác và content signature của parser. Không đổi stored HTML hoặc thuật toán trích xuất. Tất cả comparisons có ordering ổn định sau DB round-trip.

**Tests:** navigation 404/410 vs AJAX/ảnh 404, retry cuối cùng, 403/429/timeout không unavailable, unsafe/cross-host redirect, node identity mismatch; metadata UUID/provenance/time/parser-version/list ordering đổi vẫn hash bằng nhau; title/giá/chỗ/ma trận giá/day text/link đích/query/fragment/ảnh/rowspan/null-zero đổi làm hash khác; variants/xung đột không bị dedup; source và UI state đã whitelist không tạo delta giả; hash trước/sau DB round-trip bằng nhau.

**Vì sao:** tránh đánh dấu unavailable bằng lỗi không liên quan và tránh delta giả mỗi lần UUID/time thay đổi.

**Rủi ro:** fingerprint omit field gây false unchanged, hoặc hashing volatile UI tạo update giả. Ưu tiên giữ ngữ nghĩa, kiểm thử từng field nhóm và version hash độc lập với parser.

### 7. Orchestrator incremental, tái dùng baseline và transaction checkpoints

**Files:**
- `src/hanoitourist_crawler/incremental.py` **mới**
- `src/hanoitourist_crawler/pipeline.py`
- `src/hanoitourist_crawler/storage.py`
- `tests/test_incremental.py` **mới**
- `tests/test_pipeline.py`
- `tests/test_catalogue_storage.py`

**Thay đổi:** thêm `crawl_domestic(config, storage, ..., scheduled_for=None)` với collector/discovery/clock injection để test offline. Trình tự:

1. Preflight, lấy session lock; scheduled caller kiểm tra lại mốc đã xử lý. Busy trả kết quả skipped/busy, không tạo batch và không gọi browser.
2. Dưới lock phục hồi batch running bỏ dở; tạo batch mới và discovery run. Seed baseline lịch sử idempotent cho scope đã chứng minh.
3. Persist discovery positive inventory, bind URL aliases, ghi danh sách lựa chọn và lý do. Không đánh dấu available chỉ từ card.
4. Với từng selected tour, cap budget theo phần còn lại, tạo detail run và checkpoint run ref vào batch **trước** collect. Dùng `crawl/parse_run` hiện có để giữ staging/snapshot/replay; không tạo pipeline parser thứ hai hoặc scrape rút gọn.
5. Với complete snapshot, validate expected identity, hash và publish accepted state + batch outcome/change_kinds trong một transaction ngắn. Unchanged vẫn cập nhật watermark và snapshot kiểm chứng, nhưng không tạo delta. Partial/failed giữ accepted snapshot và watermark cũ; record latest attempt và retry-needed. 404/410 update availability bằng evidence, không coi là complete parse.
6. Resolve identity/aliases trước reconciliation; nếu discovery complete, đánh dấu tour đã crawl không có trong discovered entity set là unavailable. Repeated unavailable không phát sinh event lặp.
7. Freeze batch rows cho full/delta từ state hiện tại bằng một transaction, gồm tour không chọn và unavailable. Finish crawl status/counters; export riêng ở bước 8.

Legacy `crawl` dùng cùng lock cho toàn capture/parse, nhưng không thay đường dẫn/JSON/exit contract. Tách helper nội bộ nếu cần tránh acquire lock hai lần khi batch gọi pipeline cho từng tour; lock phải có một owner và release cân bằng ở finally. Không giữ SQL transaction khi browser chờ mạng. `parse` replay không tiến registry watermark hoặc scheduler state.

Abandoned recovery dưới lock: terminalize running detail run/batch có cấu trúc lỗi, giữ mọi committed raw/snapshot; nếu snapshot complete đã persist nhưng state publication chưa commit thì apply outcome idempotently từ linked run/evidence, không crawl lại. Freeze các kết quả đã có, các candidates chưa làm giữ pending; xuất batch partial này để không làm mất delta đã commit trước crash. Không tái claim slot hoặc overwrite event cùng batch/tour.

**Tests:** baseline complete mới nhất theo capture time và baseline partial; first batch tận dụng Phú Quốc thay vì crawl lại nếu chưa đến hạn; chính xác biên 7 ngày UTC, 6 ngày không fetch; tour mới/failed/reappeared lấy ngay; unchanged vs price/content changed; disappearance complete/partial/empty; unavailable reappear thành công/thất bại; lỗi một tour không chặn các tour sau; alias canonical/slug cùng source ID không new giả; new partial hiển thị pending; budget/interruption/recovery đúng checkpoints; hai tiến trình manual/scheduled không chồng; không mất delta sau crash giữa persist snapshot/apply state/export.

**Vì sao:** đây là state machine incremental chính, nối các contracts hiện có mà không thay semantics parser và lịch sử.

**Rủi ro:** crash giữa capture và registry update, retries kéo watermark sai, seed báo mọi tour mới giả hoặc absence chạy quá sớm. Các điểm publish/reconcile/freeze phải idempotent và có PostgreSQL integration tests, không chỉ MemoryStorage happy path.

### 8. Export delta/full XLSX cố định theo batch, atomic và offline

**Files:**
- `src/hanoitourist_crawler/excel.py`
- `src/hanoitourist_crawler/storage.py`
- `tests/test_excel.py`
- `tests/test_export.py`

**Thay đổi:** factor row-building tối thiểu để workbook nhiều tour dùng lại formatting/safety/content rules của exporter một tour. Giữ `export_excel(storage, run_id, output_dir, version=None)` và output lịch sử nguyên trạng. Thêm `export_batch_excel(storage, batch_id, output_dir)` đọc **frozen batch rows + exact snapshot IDs**, không crawl/parse/update current data.

Hai outputs: `exports/domestic/<batch-id>/delta.xlsx`, `full.xlsx`. Sáu sheet nghiệp vụ giữ full content trong một ô/day/variant, shared price không nhân, raw/normalized money, IDs liên kết và không hyperlink booking; thêm `Nhật ký tải` cho batch metadata/outcomes/errors. Bản batch thêm các cột rõ nghĩa: availability/reason, listed, data quality, change kind, last successful check, attempt error và stable catalogue ID. Không dùng chung chữ “Trạng thái” cho coverage và availability. Tour chưa đủ snapshot vẫn có summary, các child rows chỉ từ snapshot thật; dữ liệu cũ chưa kiểm tra lại phải thể hiện timestamp cũ.

Build cả hai workbook trong temporary sibling directory, fsync, rồi publish cả directory khi cả hai đã thành công. Chỉ sau publish mới cập nhật export_manifest/status trong DB; manifest lưu batch/frozen-selection/export-schema version và checksums để nhận diện bundle đã publish nếu crash trước DB acknowledgment. Directory thành công là immutable; export lại cùng frozen selection/schema vào cùng destination trả bundle hiện có sau kiểm tra, không replace directory nonempty. Nếu đổi destination thì tạo bundle mới. Bundle khác schema/hỏng phải báo lỗi và hướng dẫn output-dir khác, không âm thầm overwrite lịch sử.

Thất bại giữ current registry/raw/snapshot và bundle cũ, cleanup temporary, ghi export failure an toàn. Published bundle chưa được DB acknowledge có thể được xác nhận lại offline. Không rollback business state và không recrawl website vì lỗi Excel.

**Tests:** multi-tour đầy đủ, delta chỉ changed và full gồm skipped/unavailable; baseline/cached timestamps đúng; empty delta/failed-discovery được annotate; batch cũ re-export sau batch mới không đổi nội dung/ref; mixed parser versions của các tour được ghi riêng, không nhầm một version toàn workbook; seven batch sheets vs six legacy sheets; pending/partial giữ dữ liệu thật; formula/precision/null/zero/rowspan/variants/link/32767 chars/XML/row limits; lỗi workbook thứ hai không publish bundle; fsync/rename/DB acknowledgment failure recovery; DB/export không đổi raw/snapshot/current registry.

**Vì sao:** full phải giữ dữ liệu tour không crawl trong lượt này; delta phải xem được toàn bộ thông tin tour thay đổi và có thể tái xuất đúng batch lịch sử.

**Rủi ro:** query latest khi re-export làm historical full thay đổi, hai rename file riêng không atomic như một bundle, workbook lớn vượt Excel limits. Báo lỗi rõ, không truncate/split nội dung tự động. Không redesign giao diện workbook ngoài các cột/nhật ký cần thiết.

### 9. CLI và scheduler bền vững với catch-up

**Files:**
- `src/hanoitourist_crawler/cli.py`
- `src/hanoitourist_crawler/scheduler.py` **mới**
- `src/hanoitourist_crawler/incremental.py`
- `tests/test_cli.py`
- `tests/test_scheduler.py` **mới**

**Thay đổi:** thêm lệnh:

```text
hanoitourist crawl-domestic [--headed] [--output-dir DIR]
hanoitourist export-batch --batch-id UUID [--output-dir DIR]
hanoitourist schedule
```

`crawl-domestic` chạy batch và xuất cả hai XLSX; `export-batch` chỉ đọc batch frozen; `schedule` gọi cùng orchestrator/exporter, không shell cron hay subprocess CLI để tránh lệch behavior. Lệnh `crawl`, `parse`, `export --format json/xlsx`, `db init` cũ giữ flags/defaults.

Scheduler loop có clock/sleeper/storage factory injection, mở lại DB connection sau sleep/restart/mất kết nối; kiểm tra latest due slot ở mỗi tick thay vì tin timer trong RAM. Cập nhật `last_handled_slot` khi invocation terminal, kể cả partial/failed/export failure đã ghi vào batch để không retry nóng. Busy đợi tick sau; config/DB errors log an toàn với backoff giới hạn, không consume slot khi chưa tạo/xử lý batch. SIGTERM/KeyboardInterrupt dừng gọn, finally giải phóng connection/lock; không dùng credential/exception message tùy ý trong log.

CLI in batch ID, discovery/crawl/export statuses, new/updated/unavailable/reactivated/unchanged/skipped/partial/failed counts và hai path. Exit 0 complete, 2 partial/busy, 1 failed/export/config/DB error, 130 interrupted. Log scheduler giữ outcome nhưng không thoát daemon chỉ vì một tour failed.

**Tests:** parsing/help không cần DB, legacy tests nguyên contract, exit codes và secret redaction; manual batch/export offline; timezone UTC-vs-VN midnight và boundary; first start/catch-up nhiều ngày một lượt; restart cùng ngày không trùng; máy sleep vượt mốc; time jump backward không rerun; giờ cấu hình đổi có documented nearest-slot policy; failed slot không loop nóng; busy không consume slot; DB disconnect/reopen; stopped mid-run recovery và partial/export failure không mất status.

**Vì sao:** cron thuần bỏ lỡ khi máy ngủ và không đủ state cho catch-up; scheduler cần durable checkpoint và cùng concurrency guard với chạy thủ công.

**Rủi ro:** duplicate run do check slot trước lock, đánh dấu lịch đã chạy trước khi có batch, hoặc ngủ nhiều ngày kéo backlog vô hạn. UTC storage + local slot computation + recheck dưới lock là bắt buộc.

### 10. Đóng gói Docker Compose cho máy cá nhân

**Files:**
- `Dockerfile` **mới**
- `.dockerignore` **mới**
- `compose.yaml`
- `.env.example`
- `README.md`

**Thay đổi:** image Python 3.13 slim, install package bình thường và `python -m playwright install --with-deps chromium` khi build để browser khớp Playwright được cài; có OS timezone data cho ZoneInfo. Không copy `.git`, `.env`, `.venv`, `.codegraph`, exports/build/caches vào image. Chạy headless, user không root nếu filesystem/browser hỗ trợ, bind-mounted exports có quyền ghi rõ ràng.

Thêm service `crawler` profile `scheduler`, command `hanoitourist schedule`, `init: true`, `shm_size` phù hợp Chromium, restart policy và dependency PostgreSQL healthy. Dùng PostgreSQL host `postgres:5432` trong container, không dùng `DATABASE_URL` localhost của host; `.env.example` có `SCHEDULER_DATABASE_URL` riêng và hướng dẫn percent-encode credentials khi cần. Mount `./exports` tới export directory; postgres_data hiện tại giữ nguyên. Profiles đảm bảo `docker compose up` cũ không tự mở browser/crawl khi người dùng chỉ muốn DB.

Hướng dẫn startup theo thứ tự: PostgreSQL healthy -> build crawler -> `run --rm crawler db init` -> `up -d crawler`. Schema migration là lệnh explicit; scheduler preflight không tự sửa schema. Đổi lịch bằng env rồi recreate service, không rebuild code. Máy ngủ/tắt vẫn không thể chạy đúng 00:00; khi Docker/service hoạt động lại mới có catch-up. Không hứa Docker Desktop tự mở sau reboot nếu người dùng chưa bật tùy chọn đó.

**Kiểm chứng:** `docker compose --profile scheduler config`, image build, container `--help`, init vào DB kiểm chứng, một batch mock/manual và restart service để xác nhận persistent slot/exports. Kiểm tra profile mặc định chỉ có postgres, host/container DSN đúng, logs không lộ DSN, Chromium/ZoneInfo/export permissions. Không `down -v`, không reset hoặc đổi project/port của DB hiện có.

**Vì sao:** triển khai được trên máy cá nhân mà sau này chuyển server không phải đổi logic scheduler.

**Rủi ro:** Chromium dependencies/architecture, timezone data, UID permissions, Docker Desktop resources, credentials host-vs-container và restart khi chưa migrate. Ghi rõ phần smoke chưa chạy nếu Docker không khả dụng.

### 11. Version, tài liệu, regression và nghiệm thu cuối

**Files:**
- `pyproject.toml`
- `src/hanoitourist_crawler/__init__.py`
- `README.md`
- Tất cả tests mới/cập nhật đã liệt kê ở bước 2–9.

**Thay đổi:** package lên `0.2.0`, mô tả domestic incremental + legacy CLI. `COLLECTOR_VERSION=0.2.0` vì navigation manifest/scope capture thay đổi; **giữ `PARSER_VERSION=0.1.4`** nếu không đổi logic extraction/validation. Fingerprint `v1` và schema export batch version riêng; nếu thực sự phải đổi parser để hỗ trợ markup tour mới thì review thay đổi đó và tăng parser version, không sửa snapshots version cũ.

README giữ các nghiệm thu lịch sử và giới hạn an toàn; thay phần “duy nhất một tour/chưa có scheduler” bằng scope mới, mô tả baseline reuse/TTL/availability/quality/delta/full/batch refs. Hướng dẫn Python local, Compose profile, migration có dữ liệu cũ, lịch/catch-up/busy, đổi giờ, full-data constraints và re-export khi Excel lỗi. Ghi baseline Git SHA/kết quả implementation đúng sự thật; không ghi trước kết quả test/live chưa chạy.

**Thứ tự verification trong IMPLEMENT:**

1. Hoàn tất Git baseline ở bước 1 trước mọi edits, rồi chạy regression mới trên code cũ để xác nhận các case incremental/CLI chưa được hỗ trợ; không ép fixtures lịch sử đổi để xanh.
2. Reinstall package thường sau source changes theo README (không `-e` trên môi trường macOS này), xác nhận runtime/source versions.
3. Offline unit: `.venv/bin/python -m pytest -m 'not browser and not postgres' -q`.
4. Browser mocks: `.venv/bin/python -m pytest -m browser -q` với Chromium thật, không website live trong pytest.
5. Full suite với `TEST_DATABASE_URL` trên schema UUID cô lập; DB kiểm chứng README hiện tại dùng port 55432, không động vào dịch vụ khác ở 5432. Browser/Postgres skips phải báo rõ và không tính như đã nghiệm thu.
6. Syntax/package/migration resource checks; run CLI từ ngoài repo để xác nhận migration và package data được đóng gói. Kiểm chứng Compose/Docker ở bước 10.
7. Live smoke read-only danh mục trước, đối chiếu page/card count/terminal; rồi batch thật theo scope đã chốt, quan sát đầy đủ vài tour khác nhau. Nếu tour/template không được adapter hỗ trợ thì ghi partial và giữ dữ liệu cũ, không báo full giả. Không truy cập booking hoặc submit form.
8. Batch thứ hai chưa đủ 7 ngày: listing vẫn được quét, complete tours unchanged không bị tải chi tiết lại, delta rỗng nếu site không đổi. Thử new/due/removed/reappeared và catch-up bằng fixtures/fake clock, không sửa DB lịch sử thật hay chờ 7 ngày để kiểm thử.
9. Kiểm tra workbook full/delta từ DB, offline re-export batch cũ sau batch mới; raw/history/amount/time không đổi, lỗi export không recrawl.
10. `codegraph sync` sau implementation, kiểm tra affected symbols/tests và cập nhật README bằng kết quả thực tế. Review `git diff` so với baseline commit và báo exact file list/test results; không amend/reset baseline để giấu thay đổi.

**Vì sao:** nghiệm thu phải bao phủ browser/DB/concurrency/scheduler/Excel và regression chức năng cũ, không chỉ unit state machine.

**Rủi ro:** chạy nhầm installed package cũ, migrations không packaged, live site/access thay đổi, DB/Docker/Chromium không sẵn. Báo phần chưa kiểm chứng; không dùng live failure để che regression offline.

## Danh sách file dự kiến chạm

### Git baseline, chỉ ở IMPLEMENT

- `.git/` (repository metadata mới, có `.git/info/exclude` cho CodeGraph generated).
- Stage/commit các file hiện có theo bước 1; không sửa chúng để tạo baseline.

### Sửa source/config/docs

- `src/hanoitourist_crawler/__init__.py`
- `src/hanoitourist_crawler/cli.py`
- `src/hanoitourist_crawler/collector.py`
- `src/hanoitourist_crawler/config.py`
- `src/hanoitourist_crawler/excel.py`
- `src/hanoitourist_crawler/models.py`
- `src/hanoitourist_crawler/pipeline.py`
- `src/hanoitourist_crawler/selectors.py`
- `src/hanoitourist_crawler/storage.py`
- `pyproject.toml`
- `compose.yaml`
- `.env.example`
- `README.md`

### Thêm source/deployment

- `src/hanoitourist_crawler/scope.py`
- `src/hanoitourist_crawler/discovery.py`
- `src/hanoitourist_crawler/fingerprint.py`
- `src/hanoitourist_crawler/incremental.py`
- `src/hanoitourist_crawler/scheduler.py`
- `src/hanoitourist_crawler/sql/002_incremental.sql`
- `Dockerfile`
- `.dockerignore`

### Sửa tests

- `tests/conftest.py`
- `tests/test_cli.py`
- `tests/test_collector.py`
- `tests/test_config.py`
- `tests/test_excel.py`
- `tests/test_export.py`
- `tests/test_parser.py`
- `tests/test_pipeline.py`
- `tests/test_storage.py`

### Thêm tests/fixtures

- `tests/test_scope.py`
- `tests/test_discovery.py`
- `tests/test_fingerprint.py`
- `tests/test_catalogue_storage.py`
- `tests/test_incremental.py`
- `tests/test_scheduler.py`
- `tests/fixtures/domestic_listing_page_1.html`
- `tests/fixtures/domestic_listing_page_2.html`
- `tests/fixtures/dynamic_domestic_listing.html`

Không dự kiến sửa logic `parser.py`, `validation.py`, `normalize.py`, `errors.py`, `export.py`, `sql/001_initial.sql`, `.gitignore` hiện có, fixtures tour lịch sử, build artifacts hoặc exports cũ. Nếu khảo sát live buộc đổi extraction, phải ghi rõ ảnh hưởng/fixture/parser version trước khi thêm vào implementation; không lồng refactor hay các features website chưa chốt vào task incremental.

## Tiêu chí hoàn thành

- Có baseline Git commit của trạng thái ban đầu trước mọi implementation edits, SHA được báo lại; không secrets/runtime artifacts/DB data vào Git và không push remote.
- Scope chỉ danh mục tour trong nước; discovery có chứng cứ coverage/pagination và không bỏ card rồi báo complete.
- Dữ liệu cũ được tận dụng, watermark dựa vào capture thành công thật; replay/export không kéo dài TTL. Tour chưa đến hạn không có request chi tiết; tour mới/due/reappeared lấy đủ qua pipeline hiện có.
- Không false unavailable khi listing partial hoặc network/parse lỗi; unavailable có evidence/reason, reactivation chỉ khi complete. Tour hết chỗ/hết lịch không tự bị đánh dấu unavailable.
- Failed/partial không thay complete baseline hoặc tiến TTL; full phản ánh cached/incomplete rõ ràng, không bù/chắp dữ liệu để báo đủ.
- Delta không nhiễu UUID/time/UI đã whitelist, bắt được thay đổi nghiệp vụ và trạng thái; full gồm toàn bộ entity trong scope chưa superseded. Re-export batch cũ offline giữ refs và nội dung lịch sử.
- Hai XLSX publish thành bundle thành công hoặc ghi export failure rõ ràng; lỗi file không rollback data hay gây recrawl.
- Scheduler đúng 00:00 Việt Nam, cấu hình được, persistent catch-up một lượt, không duplicate slot/chạy chồng; restart/crash giữ committed history và không làm mất các changes đã publish trong DB.
- Migration additive/idempotent giữ dữ liệu cũ; legacy CLI/export/tests vẫn hoạt động; package/browser/image/migration resource được kiểm chứng.
- Regression/unit/browser/Postgres/concurrency/scheduler tests có kết quả thực, phần live/Docker hoặc tests chưa chạy được báo minh bạch.

## Điểm dừng ARCHITECT

Kế hoạch mới này thay thế `PLAN.md` lịch sử về các fix và export một tour đã hoàn thành. Chỉ `PLAN.md` được ghi trong bước ARCHITECT. Flow tự chuyển sang IMPLEMENT; thao tác đầu tiên tại đó là Git baseline ở bước 1, rồi mới sửa code. Không chạy lệnh handoff thủ công trong flow đang hoạt động.
