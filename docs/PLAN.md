# IDMClone — Phân tích & Kế hoạch triển khai

> Trạng thái: **P0 → P6 đã hoàn thành** (2026-08-12) — toàn bộ lộ trình. Xem
> [../README.md](../README.md) để biết cách chạy và cách dựng bản cài đặt.

## 1. Bối cảnh & mục tiêu

Xây dựng một trình quản lý tải xuống cho Windows với tập tính năng tương đương Internet Download Manager (IDM): tải đa luồng có phân đoạn, tạm dừng/tiếp tục, bắt link từ trình duyệt, tải video streaming, lên lịch, và quản lý hàng đợi.

Mục tiêu chất lượng:
- **Tốc độ**: nhanh hơn tải trực tiếp bằng trình duyệt ≥ 2–3 lần trên server hỗ trợ HTTP Range.
- **An toàn dữ liệu**: mất điện / kill process không làm hỏng file đang tải — luôn resume được.
- **Trải nghiệm**: người dùng bấm link trong Chrome/Edge → app tự bắt và tải, giống hệt IDM.

Ràng buộc kỹ thuật đã xác nhận trên máy: Python 3.13.14, Node 22.20, Rust 1.96 (không có .NET/Go). Thư mục `E:\idmclone` hiện trống — dự án greenfield.

**Lưu ý pháp lý**: chỉ clone *tính năng*. Không sao chép mã nguồn, icon, tên thương hiệu hay giao thức riêng của IDM. Tên sản phẩm nội bộ dùng "IDMClone" (đổi tên trước khi phát hành).

## 2. Phân rã tính năng IDM → hạng mục kỹ thuật

| Tính năng IDM | Cơ chế kỹ thuật cần làm |
|---|---|
| Tải nhanh đa luồng | Chia file thành N đoạn qua HTTP `Range`, tải song song, ghi vào cùng 1 file theo offset |
| Dynamic segmentation | Đoạn nào xong trước thì "cắt" phần còn lại của đoạn chậm nhất → không bị 1 luồng rùa kéo lùi |
| Resume | Sidecar metadata `.idmdown` ghi tiến độ từng đoạn + `ETag`/`Last-Modified` để xác thực file trên server không đổi |
| Bắt link trình duyệt | Extension MV3 (Chrome/Edge) + Native Messaging host, chặn download rồi chuyển URL + cookie + header sang app |
| Download this video | Extension sniff request `.m3u8`/`.mpd`/`video/*` → hiện nút nổi trên trang |
| Tải YouTube/HLS/DASH | Nhúng `yt-dlp` (dạng thư viện) + `ffmpeg` để merge audio/video |
| Hàng đợi & Scheduler | Bảng `queues`/`schedules` trong SQLite + vòng lặp scheduler; hành động sau khi xong (tắt máy/thoát/hibernate) |
| Phân loại tự động | Map phần mở rộng → thư mục đích (Video/Music/Compressed/Documents/Programs) |
| Site Grabber | Crawler theo depth + filter đuôi file/regex, xuất danh sách rồi đẩy vào hàng đợi |
| Giới hạn tốc độ | Token bucket toàn cục + theo từng task |
| Tích hợp hệ thống | Theo dõi clipboard, kéo-thả, tray icon, khởi động cùng Windows, single-instance IPC |

## 3. Kiến trúc tổng thể

```
┌──────────────────────────────────────────────────────────┐
│  Chrome/Edge Extension (MV3)                             │
│   • chặn download  • sniff media  • gửi cookie/header    │
└───────────────┬──────────────────────────────────────────┘
                │ Native Messaging (stdio, JSON có length prefix)
┌───────────────▼──────────────────────────────────────────┐
│  native_host.py  →  QLocalSocket  →  instance app đang chạy│
└───────────────┬──────────────────────────────────────────┘
                │
┌───────────────▼──────────────────────────────────────────┐
│  GUI (PySide6, main thread)                              │
│   MainWindow · TaskTable · Dialogs · Tray                │
└───────────────┬──────────────────────────────────────────┘
                │ Qt Signals / queue (thread-safe)
┌───────────────▼──────────────────────────────────────────┐
│  Engine (1 thread nền chạy asyncio event loop)           │
│   Scheduler → TaskRunner → SegmentWorker(httpx)          │
│   RateLimiter · Writer(pwrite) · ResumeStore(.idmdown)   │
└───────────────┬──────────────────────────────────────────┘
                │
        SQLite (state bền vững)  +  yt-dlp / ffmpeg (subprocess)
```

**Quyết định kiến trúc quan trọng**: engine chạy **asyncio + httpx** trong *một* thread nền, thay vì thread-per-segment. Lý do: 10 task × 16 đoạn = 160 kết nối — với thread thì tốn ~160 OS thread và context-switch nặng; với asyncio chỉ là 160 coroutine. GUI không bao giờ gọi trực tiếp vào engine: mọi lệnh (start/pause/cancel) đẩy qua `asyncio.run_coroutine_threadsafe`, mọi cập nhật tiến độ trả về GUI qua Qt signal, gom nhóm 250ms/lần để không làm nghẽn UI.

## 4. Tech stack

- **Ngôn ngữ**: Python 3.13
- **GUI**: PySide6 (Qt 6, giấy phép LGPL — an toàn cho phần mềm đóng)
- **HTTP**: `httpx` (async, HTTP/2, proxy, cookie) + `certifi`
- **Media**: `yt-dlp` (thư viện), `ffmpeg.exe` (đóng gói kèm, bản LGPL build)
- **DB**: `sqlite3` chuẩn (WAL mode)
- **Crawler**: `selectolax` hoặc `beautifulsoup4` + `lxml`
- **Test**: `pytest`, `pytest-asyncio`, server HTTP giả lập hỗ trợ Range
- **Đóng gói**: PyInstaller (one-dir) + Inno Setup (installer + đăng ký native messaging host)

## 5. Cấu trúc thư mục dự kiến

```
E:\idmclone\
├─ app\
│  ├─ main.py                  # entrypoint, single-instance, parse argv
│  ├─ core\
│  │  ├─ engine.py             # event loop nền, quản lý vòng đời task
│  │  ├─ scheduler.py          # hàng đợi, giới hạn concurrent, hẹn giờ
│  │  ├─ task.py               # DownloadTask + state machine
│  │  ├─ segment.py            # SegmentWorker + thuật toán dynamic split
│  │  ├─ probe.py              # HEAD/Range probe: size, accept-ranges, tên file
│  │  ├─ http_client.py        # client dùng chung: proxy, UA, referer, auth, cookie
│  │  ├─ writer.py             # cấp phát file trước + ghi theo offset
│  │  ├─ resume.py             # đọc/ghi .idmdown, xác thực ETag
│  │  ├─ ratelimit.py          # token bucket
│  │  └─ categories.py         # map đuôi file → thư mục đích
│  ├─ media\  (detect.py, m3u8.py, hls.py, ytdlp.py, ffmpeg.py, runner.py)
│  ├─ grabber\crawler.py
│  ├─ storage\ (db.py, repo.py, settings.py)
│  ├─ ipc\ (single_instance.py, native_host.py, clipboard_watch.py)
│  ├─ ui\ (main_window.py, task_model.py, add_url_dialog.py,
│  │       progress_dialog.py, settings_dialog.py, tray.py, resources\)
│  └─ util\ (fmt.py, paths.py, log.py)
├─ extension\ (manifest.json, background.js, content.js, popup\)
├─ tests\
├─ packaging\ (idmclone.spec, installer.iss, native_host_manifest.json)
└─ docs\PLAN.md
```

## 6. Thiết kế lõi tải xuống (phần khó nhất)

### 6.1 Probe
1. `HEAD` URL (theo redirect). Nếu server chặn HEAD → `GET` với `Range: bytes=0-0`.
2. Thu thập: `Content-Length`, `Accept-Ranges`, `ETag`, `Last-Modified`, `Content-Type`, tên file từ `Content-Disposition` (hỗ trợ RFC 5987 `filename*`), fallback lấy từ path URL.
3. Xác nhận hỗ trợ đa đoạn: phải trả `206 Partial Content` **và** `Content-Range` khớp. Nếu không → hạ về tải 1 luồng (vẫn stream ra đĩa, vẫn resume được nếu về sau server hỗ trợ Range).

### 6.2 Chia đoạn & ghi file
- Cấp phát file trước bằng `f.truncate(size)` (NTFS tạo sparse file — không tốn đĩa ngay).
- Mỗi segment mở **file descriptor riêng** và ghi theo offset tuyệt đối → ghi song song không cần lock. (Thực tế khi code: `os.pwrite` **chỉ có trên Unix**, nên trên Windows dùng `lseek` + `write` trên fd riêng của từng segment — an toàn chính vì fd không dùng chung. Xem `app/core/writer.py`.)
- Buffer 1 MB/segment trước khi flush, giảm số syscall.
- Số đoạn mặc định 8, cho phép 1–32. File < 1 MB → 1 đoạn.

### 6.3 Dynamic segmentation (điểm ăn tiền của IDM)
Khi một segment hoàn thành, thay vì để luồng đó chết:
1. Tìm segment còn lại nhiều byte chưa tải nhất.
2. Nếu phần còn lại > ngưỡng (2 MB), cắt đôi phần *chưa tải*: `victim.end = mid`, tạo segment mới `[mid+1, old_end]` giao cho worker rảnh.
3. Việc cắt phải thực hiện dưới `asyncio.Lock` và kiểm tra lại `downloaded` ngay tại thời điểm cắt để tránh race.

### 6.4 Resume
- Sidecar `<tên file>.idmdown` (JSON) cạnh file đích, ghi atomic (write tmp → `os.replace`), flush mỗi 1s hoặc mỗi 4 MB.
```json
{ "v":1, "url":"...", "final_url":"...", "size":734003200,
  "etag":"\"a1b2\"", "last_modified":"...", "accept_ranges":true,
  "segments":[{"start":0,"end":91750399,"done":91750399}, ...] }
```
- Khi resume: probe lại; nếu `ETag`/`Last-Modified`/`size` khác → hỏi người dùng "tải lại từ đầu?".
- Xong 100% → xoá `.idmdown`, đổi tên `.part` → tên thật, di chuyển vào thư mục danh mục.

### 6.5 Lỗi & retry
- Backoff luỹ thừa có jitter: 1s → 2s → 4s → 8s (tối đa 5 lần / segment).
- Phân loại lỗi: mạng tạm thời (retry) · 401/403 (hỏi credential) · 404/410 (fail hẳn) · 416 (metadata sai → reset segment) · đĩa đầy (pause toàn bộ).
- Timeout: connect 15s, read 30s (áp cho từng chunk, không cho cả request).

### 6.6 Giới hạn tốc độ
Token bucket toàn cục dùng chung, các segment `await bucket.acquire(n)` trước khi ghi. Tránh sleep cố định (gây giật), dùng cấp phát token theo thời gian thực.

## 7. Lưu trữ trạng thái

SQLite (`%LOCALAPPDATA%\IDMClone\idmclone.db`, WAL):
- `downloads` — id, url, final_url, filename, save_path, size, downloaded, state, category, queue_id, speed_limit, added_at, finished_at, error
- `download_headers` — cookie/referer/UA/auth theo từng task
- `queues`, `schedules` — hàng đợi và lịch chạy
- `settings` — key/value
- `history` — log kết quả

Tiến độ chi tiết từng đoạn **không** lưu trong DB (ghi quá thường xuyên) mà nằm ở file `.idmdown`; DB chỉ giữ tổng số byte, đồng bộ mỗi 2s.

## 8. Giao diện (PySide6)

- **MainWindow**: toolbar (Add URL · Resume · Stop · Stop All · Delete · Options · Scheduler) + cây danh mục bên trái + `QTableView` bên phải (Tên · Kích thước · Trạng thái · Còn lại · Tốc độ · Ngày). Dùng `QAbstractTableModel` tuỳ biến; cập nhật theo lô 250ms để không lag khi có 100+ task.
- **Add URL dialog**: URL, thư mục lưu, danh mục, số kết nối, referer/cookie/UA, nút "Download Later".
- **Progress dialog**: thanh tiến độ tổng + biểu đồ tốc độ + hiển thị trạng thái từng segment (giống IDM), tuỳ chọn hành động khi xong.
- **Options**: kết nối, proxy, danh mục, tích hợp trình duyệt, giới hạn tốc độ, ngôn ngữ (VI/EN).
- **Tray icon**: hiện tốc độ tổng, menu nhanh; drop-target nổi để kéo-thả link.

## 9. Tích hợp trình duyệt

**Extension MV3** (`extension/`):
- `chrome.downloads.onDeterminingFilename` → `chrome.downloads.cancel()` → gửi `{url, filename, referrer, cookies, userAgent}` sang native host.
- `chrome.webRequest.onBeforeRequest` lọc `.m3u8/.mpd/.mp4` → lưu vào danh sách media của tab → content script hiện nút nổi "Tải video này".
- Popup: bật/tắt bắt link, danh sách đuôi file loại trừ.

**Native host** (`app/ipc/native_host.py`): đọc stdio theo chuẩn native messaging (4 byte little-endian length + JSON UTF-8), forward sang instance app qua `QLocalSocket`; nếu app chưa chạy thì khởi động nó. Installer ghi registry `HKCU\Software\Google\Chrome\NativeMessagingHosts\com.idmclone.host` (và nhánh Edge tương ứng).

## 10. Video streaming

- **YouTube & 1000+ site**: gọi `yt-dlp` ở chế độ thư viện để lấy danh sách format (không tải bằng nó), rồi **đưa direct URL về engine đa luồng của mình** → nhanh hơn yt-dlp mặc định. Với DASH: tải riêng video track + audio track, sau đó `ffmpeg -c copy` để merge.
- **HLS (.m3u8)**: parser riêng — đọc master playlist → chọn bitrate → tải các `.ts` song song (mỗi segment là một task con) → giải mã AES-128 nếu có `#EXT-X-KEY` → concat → remux sang `.mp4` bằng ffmpeg.
- ffmpeg dò theo thứ tự: thư mục app → PATH → nhắc người dùng tải.

**Đã làm (P4) — vài chỗ khác thiết kế ban đầu:**

- `MediaTaskRunner` (`app/media/runner.py`) dùng đúng bốn phương thức của `TaskRunner`
  nên `Engine` chỉ cần chọn lớp runner theo `DownloadRequest.media_kind`; hàng đợi,
  token bucket và GUI không biết gì về playlist.
- Resume của HLS **không** dùng sidecar: mỗi segment ghi ra `NNNNNN.part` rồi
  `os.replace` sang `NNNNNN.bin`. Có tên chính thức nghĩa là đủ byte, nên chỉ cần
  liệt kê thư mục là biết còn thiếu segment nào. Sidecar `.idmdown` chỉ dùng cho
  các track tải bằng `TaskRunner` (video/audio direct URL).
- Ghép file làm bằng **nối byte** (`stream.raw`) rồi mới remux, thay vì concat
  demuxer của ffmpeg: đúng cho cả MPEG-TS lẫn fMP4 có `#EXT-X-MAP`, và vẫn ra file
  xem được khi máy không có ffmpeg (giữ nguyên `.ts`).
- Playlist không có `Content-Length` cho cả stream, nên kích thước tổng là **ước
  lượng**: trung bình các segment đã tải nhân số segment còn lại. Số này hội tụ
  nhanh vì HLS cắt theo thời lượng cố định, và được thay bằng kích thước thật khi
  ghép xong.
- Chọn format: bản 360p **có tiếng** được ưu tiên hơn bản 1080p câm; chỉ tách
  video/audio khi có cả hai để ghép.
- Với YouTube/TikTok..., URL segment có chữ ký nên extension gửi **URL của trang**
  chứ không gửi link sniff được; danh sách host nằm ở `app/media/detect.py` và
  `extension/background.js` (hai bản phải khớp nhau).

## 10b. Hàng đợi, hẹn giờ và Site Grabber (P5)

**Đã làm — vài chỗ khác thiết kế ban đầu:**

- Hàng đợi được điều phối ở `Controller` (phía GUI) chứ không nhét vào engine:
  engine chỉ biết "chạy tối đa N task", còn thứ tự trong hàng đợi, số file cùng
  lúc và lúc nào bắt đầu file kế tiếp là chuyện của lớp trên. `pump_queues()`
  chạy ngay sau mỗi lần một task kết thúc, không đợi timer.
- `app/core/schedule.py` không có đồng hồ: mọi hàm nhận `now`. Nhờ vậy các ca
  "lỡ giờ vì máy tắt", "cửa sổ 23:00→02:00", "chỉ chạy thứ 2–6" test được bằng
  `datetime` cố định. `QueueScheduler` (QTimer 15 giây) chỉ bơm giờ thật vào.
- Chống chạy trùng bằng cột `last_run` lưu **mốc của lần chạy** chứ không phải
  thời điểm bấm nút — sửa giờ trong dialog thì `last_run` bị xoá, nếu không mốc
  mới sẽ bị coi là "đã chạy hôm nay".
- Hàng đợi **không** tự chạy lại file lỗi (tránh vòng lặp với link 404) và không
  bật lại file người dùng tự tay tạm dừng (`DownloadItem.manual_pause`).
- Hành động sau khi xong gắn với **từng hàng đợi**, không phải toàn app; lệnh
  tắt máy đi qua `app/util/power.py` với runner tiêm được, và giao diện đếm
  ngược 30 giây cho phép huỷ.
- Crawler dùng `html.parser` của stdlib thay vì `selectolax`/`beautifulsoup4`
  như dự kiến — bớt một dependency, và thứ cần lấy chỉ là `href`/`src`. Liên kết
  `<a>` quyết định đi tiếp, còn `img/video/source/...` chỉ là kết quả.
- Grabber chạy trên **event loop của engine** (`Engine.run_coroutine`) thay vì
  tạo thread/loop riêng cho GUI.
- Schema DB lên version 2 (`schedules.stop_at`, `schedules.last_run`) kèm hàm
  migration, vì `CREATE TABLE IF NOT EXISTS` không sửa bảng cũ.

## 10c. Đóng gói và phát hành (P6)

**Đã làm — vài chỗ khác thiết kế ban đầu:**

- Bản đóng gói có **ba** exe dùng chung một `COLLECT` chứ không phải một:
  `IDMClone.exe` (windowed), `idmclone-cli.exe` (console) và `idmclone-host.exe`
  (console, cho native messaging). Bản windowed không có stdio nên tự nó không
  thể làm host được.
- Tên `idmclone.exe` cho bản CLI **không dùng được**: tên tệp Windows không phân
  biệt hoa thường nên nó ghi đè `IDMClone.exe` ngay trong thư mục dist. Bản build
  đầu tiên đã dính lỗi này; giờ có test đọc thẳng file spec để canh.
- `native_host.launch_app()` phải trỏ đích danh `IDMClone.exe`: khi frozen,
  `sys.executable` chính là host, nên bản cũ tự sinh ra host mới thay vì mở app.
- Đăng ký native messaging **không** nằm trong trình cài đặt (ID của extension
  unpacked mỗi máy một khác) mà nằm ở `idmclone-cli.exe --register-host <id>` và
  ở **Tuỳ chọn → Tích hợp trình duyệt**. Khi frozen, manifest trỏ thẳng vào
  `idmclone-host.exe`, không cần file `.bat` lẫn Python.
- Auto-start dùng khoá `HKCU\...\Run` kèm cờ `--tray`. Chạy từ mã nguồn thì trỏ
  vào `idmclone-gui.exe` của venv, vì Run khởi động tiến trình ở `system32` nên
  `python -m app` không import được package.
- Cắt `opengl32sw.dll` và `PySide6/translations` khỏi payload (~26 MB) — app chỉ
  dùng QtWidgets với raster engine và có bảng chuỗi riêng.
- Icon `.ico` được sinh từ chính hàm vẽ QPainter của app; Qt không có bộ ghi ICO
  nên container ICO được ráp tay (`scripts/make_app_icon.py`).
- Trình cài đặt để **per-user** (`PrivilegesRequired=lowest`): app này cái gì
  cũng per-user (settings ở `%LOCALAPPDATA%`, autostart và đăng ký native
  messaging ở `HKCU`), mà cài ở chế độ admin thì `HKCU` là hive của admin —
  ISCC cảnh báo đúng chỗ đó ở lần biên dịch đầu.
- Đã biên dịch và chạy thử trọn vòng: cài im lặng ra thư mục tạm (107,7 MB, 4
  exe), `verify_p6.py --dist <thư mục vừa cài>` PASS cả bốn mục, gỡ im lặng sạch
  cả thư mục lẫn shortcut lẫn mục gỡ cài đặt trong registry.
- **Ký số** (`scripts/sign.py`, `build.py --sign`): ký ba exe *trước* khi Inno
  đóng gói rồi ký bản cài đặt, SHA-256 + timestamp, chạy qua
  `Set-AuthenticodeSignature` nên không cần Windows SDK. Rủi ro "antivirus cảnh
  báo file PyInstaller" ở mục 12 vì thế mới xử lý được một nửa: chữ ký hiện tại
  là **tự ký** (dev machine không có chứng chỉ CA), đủ để chống sửa file và để
  quy trình sẵn sàng, nhưng muốn SmartScreen thôi cảnh báo thì phải mua OV/EV
  trên token phần cứng hoặc dùng Azure Trusted Signing — lúc đó chỉ cần nạp
  `.pfx` rồi chạy đúng lệnh cũ.

## 11. Lộ trình theo giai đoạn

| Giai đoạn | Nội dung | Kết quả kiểm chứng được |
|---|---|---|
| **P0** — Khung sườn ✅ | Cấu trúc thư mục, `pyproject.toml`, logging, SQLite schema, test HTTP server hỗ trợ Range | `pytest` chạy xanh, DB tạo được |
| **P1** — Engine lõi ✅ | probe · segment · writer · resume · retry · ratelimit · dynamic split. Có CLI `python -m app <url>` | Tải file lớn, kill process giữa chừng, chạy lại → resume đúng, checksum SHA-256 khớp (`scripts/verify_p1.py`) |
| **P2** — GUI ✅ | MainWindow + model + dialogs + tray + danh mục + kéo-thả | Thao tác đủ vòng đời: thêm → tạm dừng → tiếp tục → xong → mở thư mục |
| **P3** — Trình duyệt ✅ | Extension MV3 + native host + đăng ký registry | Bấm link trong Chrome → app tự bắt và tải |
| **P4** — Video ✅ | yt-dlp adapter + HLS parser + ffmpeg merge | Tải 1 video YouTube và 1 stream m3u8, phát được |
| **P5** — Nâng cao ✅ | Scheduler/hàng đợi, Site Grabber, đa ngôn ngữ, hành động sau khi xong | Hẹn giờ tải lúc 2h sáng, grab toàn bộ ảnh 1 site |
| **P6** — Phát hành ✅ | PyInstaller + Inno Setup, auto-start, tài liệu | Chạy installer trên máy sạch, hoạt động ngay |

## 12. Rủi ro & phương án

| Rủi ro | Xử lý |
|---|---|
| Server không hỗ trợ Range hoặc trả `Content-Length` sai | Tự hạ về 1 luồng; nếu size sai thì tải streaming đến khi EOF, bỏ preallocate |
| CDN chặn khi thấy nhiều kết nối cùng IP | Cho phép hạ số kết nối theo domain; lưu profile per-host |
| Chrome MV3 service worker bị "ngủ" | Dùng `chrome.alarms` giữ nhịp; native host là bên chủ động giữ kết nối |
| yt-dlp hỏng do site đổi API | Tách hẳn thành adapter, cho phép người dùng cập nhật yt-dlp độc lập |
| Antivirus cảnh báo file PyInstaller | Ký số installer; dùng one-dir thay one-file |
| GIL làm chậm khi ghi nhiều | Ghi đĩa trong `run_in_executor` khi buffer đầy (1 MB / 500 ms) |

## 13. Cách kiểm chứng

1. **Unit/integration**: `pytest tests/` — server giả lập trong `tests/server.py` mô phỏng: hỗ trợ Range, *không* hỗ trợ Range, ngắt giữa chừng, trả 416, đổi ETag giữa lúc resume, giới hạn tốc độ.
2. **Kiểm chứng tính đúng đắn**: tải cùng một file bằng engine (16 đoạn) và bằng `curl`, so SHA-256.
3. **Kiểm chứng resume**: `taskkill /F` khi đạt ~40%, khởi động lại → phải tiếp tục đúng vị trí, checksum cuối vẫn khớp.
4. **Đo hiệu năng**: so thời gian tải một file ≥ 500 MB giữa 1 luồng và 16 luồng.
5. **Thủ công (P3+)**: chạy `python -m app`, bấm link trong Chrome, xác nhận app bắt được.

## 14. Việc cần làm ngay khi bắt đầu code (P0 → P1)

1. `pyproject.toml` + cài `httpx pyside6 yt-dlp pytest pytest-asyncio`.
2. `tests/server.py` — HTTP server hỗ trợ Range (viết trước engine để test-driven).
3. `app/core/probe.py` → `writer.py` → `resume.py` → `segment.py` → `engine.py`.
4. CLI tối giản để chạy engine không cần GUI.
5. Test resume + checksum phải xanh trước khi động vào GUI.
