# Hanoitourist Crawler — hướng dẫn chạy

CLI Python crawl tour Hanoitourist, lưu raw/snapshot vào PostgreSQL và xuất Excel/JSON. Hướng dẫn dưới đây đủ để chạy từ máy sạch.

## Yêu cầu

- Python >= 3.11
- PostgreSQL >= 15 (hoặc Docker cho PostgreSQL local)
- Chromium của Playwright (crawler luôn cần browser, kể cả HTML discovery)

## 1. Cài đặt

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python '.[dev]'
.venv/bin/python -m playwright install chromium
cp .env.example .env
```

Không dùng `pip install -e`: macOS đánh dấu `.pth` hidden nên editable import bị bỏ qua. Sau khi sửa source, cài lại:

```bash
rm -rf build
uv pip install --python .venv/bin/python --reinstall-package hanoitourist-crawler .
```

Xóa `build/` trước khi cài lại: setuptools có thể lấy lại module cũ trong `build/lib`, khiến package cài đặt không khớp source.

Nếu đã di chuyển thư mục repo, shebang trong `.venv/bin/hanoitourist` trỏ sai đường dẫn; cài lại package như trên, hoặc chạy tạm bằng `.venv/bin/python -m hanoitourist_crawler`.

## 2. Database

Đặt `DATABASE_URL` trong `.env` tới DB có quyền tạo bảng. Không dùng DB production.

Hoặc dùng PostgreSQL local bằng Compose:

```bash
docker compose up -d --wait
.venv/bin/hanoitourist db init
```

`db init` chạy lần lượt `001_initial.sql` (raw/snapshot) và `002_incremental.sql` (catalogue/scheduler). Giữ riêng hai file này; thay đổi đổi tên bảng đã gộp vào schema tương ứng, không cần file `003` riêng.

## 3. Crawl

### Crawl catalogue tour trong nước

```bash
# Discovery best-effort rồi crawl các tour cần cập nhật
.venv/bin/hanoitourist crawl-domestic

# Hiện browser để quan sát
.venv/bin/hanoitourist crawl-domestic --headed --output-dir exports
```

### Crawl đúng tour được chỉ định

Bỏ qua discovery và TTL, chỉ tải các URL nêu ra, tuần tự, mỗi tour một lần. URL được kiểm tra scope trước khi mở browser; URL sai làm lệnh fail ngay, không ghi DB.

```bash
.venv/bin/hanoitourist crawl-domestic \
  --url https://hanoitourist.vn/pqc-kham-pha-phu-quoc-hon-ngoc-vien-dong

# Nhiều tour: lặp --url, URL trùng canonical chỉ tải một lần
.venv/bin/hanoitourist crawl-domestic \
  --url https://hanoitourist.vn/<tour-a> \
  --url https://hanoitourist.vn/<tour-b>
```

Batch explicit-target `complete` khi mọi target đã attempt và không có outcome partial/failed. Tour không được nêu giữ nguyên trạng thái cũ (row `skipped`), không bị coi là biến mất khỏi listing.

### Crawl legacy một URL

Dùng `TOUR_URL` trong `.env` (mặc định là tour Phú Quốc), không qua catalogue:

```bash
.venv/bin/hanoitourist crawl
.venv/bin/hanoitourist crawl --headed --output-dir exports
```

### Chạy offline, không network

```bash
# Dùng refs đã đóng băng của batch
.venv/bin/hanoitourist export-batch --batch-id <UUID> --output-dir exports

# Parse/export lại từ staging đã lưu
.venv/bin/hanoitourist parse --run-id <UUID>
.venv/bin/hanoitourist export --run-id <UUID>
.venv/bin/hanoitourist export --run-id <UUID> --format xlsx

# Retry các tour lỗi của một batch
.venv/bin/hanoitourist retry-domestic --source-batch-id <UUID> --url <tour-url>
```

## 4. Scheduler

Mặc định chạy 00:00 `Asia/Ho_Chi_Minh`; chỉ timezone này được chấp nhận. Đổi `SCHEDULE_TIME` trong `.env`.

```bash
# Foreground, Ctrl-C để dừng
.venv/bin/hanoitourist schedule
```

Chạy bằng Compose (profile riêng, PostgreSQL mặc định vẫn chỉ là DB):

```bash
mkdir -p exports
docker compose --profile scheduler build crawler
docker compose --profile scheduler run --rm crawler hanoitourist db init
docker compose --profile scheduler up -d crawler
docker compose logs -f crawler
```

Scheduler không tự migrate. Dừng bằng `docker compose stop crawler`; không dùng `down -v` nếu muốn giữ dữ liệu.

## 5. Output và exit code

- `exports/domestic/<batch-id>/`: `full.xlsx`, `delta.xlsx`, `.bundle.json`
- `exports/<run-id>/<parser-version>.json` hoặc `.xlsx`: kết quả một run legacy
- Exit **0**: complete; **2**: partial/busy; **1**: failed/config/DB/export; **130**: bị ngắt

Discovery HTML là best-effort nên batch catalogue thường `partial` (exit 2) dù từng tour complete. Complete từng tour không có nghĩa đã lấy đủ toàn catalogue. Crawler kiểm tra robots, không bypass 401/403/CAPTCHA.

## 6. Tests

```bash
# Unit offline, không cần DB/internet
.venv/bin/python -m pytest -m 'not browser and not postgres' -q

# Browser route mocks, cần Chromium
.venv/bin/python -m pytest -m browser -q

# PostgreSQL integration: dùng schema UUID riêng, không đụng bảng hiện hữu
TEST_DATABASE_URL='postgresql://crawler:crawler_local@localhost:55432/hanoitourist' \
  .venv/bin/python -m pytest -q
```

Không trỏ `TEST_DATABASE_URL` tới production. Pytest không gọi site live; smoke live là chạy `crawl` hoặc `crawl-domestic --url` thủ công.
