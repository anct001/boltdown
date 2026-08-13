"""Minimal two-language string table.

Qt Linguist (.ts/.qm) is the "proper" answer, but it needs a build step and an
external tool for two languages that are both maintained here anyway. `tr()`
looks up Vietnamese and falls back to the English key, so an untranslated
string degrades to readable English instead of blowing up.
"""

from __future__ import annotations

LANGUAGES = {"vi": "Tiếng Việt", "en": "English"}

_VI: dict[str, str] = {
    # toolbar / menus
    "Add URL": "Thêm URL",
    "Resume": "Tiếp tục",
    "Pause": "Tạm dừng",
    "Pause All": "Dừng tất cả",
    "Resume All": "Tiếp tục tất cả",
    "Delete": "Xoá",
    "Options": "Tuỳ chọn",
    "Scheduler": "Hẹn giờ",
    "File": "Tệp",
    "Downloads": "Tải xuống",
    "Help": "Trợ giúp",
    "Exit": "Thoát",
    "About": "Giới thiệu",
    "Open": "Mở tệp",
    "Open Folder": "Mở thư mục chứa",
    "Copy URL": "Sao chép URL",
    "Properties": "Thuộc tính",
    "Redownload": "Tải lại",
    "Remove from list": "Xoá khỏi danh sách",
    "Delete file too": "Xoá cả tệp trên đĩa",
    # table headers
    "Name": "Tên tệp",
    "Size": "Kích thước",
    "Status": "Trạng thái",
    "Progress": "Tiến độ",
    "Left": "Còn lại",
    "Speed": "Tốc độ",
    "Added": "Ngày thêm",
    "Search": "Tìm kiếm",
    # categories
    "All downloads": "Tất cả",
    "Unfinished": "Chưa xong",
    "Finished": "Đã xong",
    "General": "Khác",
    "Video": "Video",
    "Music": "Nhạc",
    "Compressed": "Nén",
    "Documents": "Tài liệu",
    "Programs": "Chương trình",
    # states
    "queued": "Đang chờ",
    "probing": "Đang kiểm tra",
    "downloading": "Đang tải",
    "paused": "Tạm dừng",
    "completed": "Hoàn tất",
    "error": "Lỗi",
    "cancelled": "Đã huỷ",
    # add url dialog
    "Add a download": "Thêm tải xuống",
    "Address:": "Địa chỉ:",
    "Save to:": "Lưu vào:",
    "File name:": "Tên tệp:",
    "Category:": "Danh mục:",
    "Connections:": "Số kết nối:",
    "Browse...": "Chọn...",
    "Advanced": "Nâng cao",
    "Referer:": "Referer:",
    "Cookie:": "Cookie:",
    "User-Agent:": "User-Agent:",
    "Proxy:": "Proxy:",
    "Speed limit:": "Giới hạn tốc độ:",
    "Download now": "Tải ngay",
    "Download later": "Tải sau",
    "Cancel": "Huỷ",
    "auto": "tự động",
    "unlimited": "không giới hạn",
    # progress dialog
    "Download progress": "Tiến độ tải xuống",
    "Transfer rate": "Tốc độ truyền",
    "Segments": "Các đoạn",
    "Time left:": "Thời gian còn lại:",
    "Transferred:": "Đã tải:",
    "Close": "Đóng",
    "Open file when done": "Mở tệp khi tải xong",
    # options dialog
    "Settings": "Cài đặt",
    "Downloads folder:": "Thư mục tải về:",
    "Default connections:": "Số kết nối mặc định:",
    "Simultaneous downloads:": "Số file tải cùng lúc:",
    "Global speed limit:": "Giới hạn tốc độ tổng:",
    "Sort files into category folders": "Xếp tệp vào thư mục theo danh mục",
    "Minimize to tray instead of closing": "Thu nhỏ xuống khay thay vì đóng",
    "Ask before every download": "Hỏi trước mỗi lần tải",
    "Verify TLS certificates": "Kiểm tra chứng chỉ TLS",
    "Language:": "Ngôn ngữ:",
    "General settings": "Cài đặt chung",
    "Connection": "Kết nối",
    "Save": "Lưu",
    "Restart required for the language change.":
        "Cần khởi động lại để đổi ngôn ngữ.",
    # messages
    "Nothing selected.": "Chưa chọn mục nào.",
    "Enter a URL": "Nhập một URL",
    "That does not look like an http(s) URL.":
        "Đây không giống một URL http(s).",
    "Delete the selected downloads?": "Xoá các mục đã chọn?",
    "The file no longer exists.": "Tệp không còn tồn tại.",
    "Downloading": "Đang tải",
    "idle": "rảnh",
    "Total speed": "Tổng tốc độ",
    "Show window": "Hiện cửa sổ",
    "Paste URL from clipboard": "Dán URL từ clipboard",
    "Browser integration": "Tích hợp trình duyệt",
    "Captured from the browser": "Bắt được từ trình duyệt",
    "Extension ID:": "ID tiện ích:",
    "32 letters from chrome://extensions": "32 chữ cái lấy ở chrome://extensions",
    "Load extension/ as an unpacked extension, then paste its ID here.":
        "Nạp thư mục extension/ dạng unpacked rồi dán ID của nó vào đây.",
    "Register": "Đăng ký",
    "Remove": "Gỡ",
    "Registered for": "Đã đăng ký cho",
    "Not registered yet": "Chưa đăng ký",
    "extension id, or a Firefox add-on id":
        "ID tiện ích, hoặc ID add-on của Firefox",
    "With the extension installed, every download you start in the "
    "browser is handed to Boltdown instead.":
        "Khi đã cài tiện ích, mọi lượt tải bắt đầu từ trình duyệt sẽ được "
        "chuyển sang Boltdown.",
    "1. Load the extension into your browser":
        "1. Nạp tiện ích vào trình duyệt",
    "2. Allow it to talk to Boltdown":
        "2. Cho phép tiện ích nói chuyện với Boltdown",
    "Chrome, Edge, Brave: Developer mode -> Load unpacked":
        "Chrome, Edge, Brave: bật Developer mode → Load unpacked",
    "Firefox: about:debugging -> Load Temporary Add-on -> manifest.json":
        "Firefox: about:debugging → Load Temporary Add-on → chọn manifest.json",
    "The Firefox folder is built by scripts/build_extension.py.":
        "Thư mục cho Firefox do scripts/build_extension.py sinh ra.",
    "Chromium gives the unpacked extension a new id every time it is "
    "loaded, so paste the id shown under its name. Firefox always uses "
    "the same one and is registered already.":
        "Chromium cấp ID mới mỗi lần nạp tiện ích dạng unpacked, nên hãy dán "
        "ID hiện dưới tên tiện ích. Firefox luôn dùng một ID cố định và đã "
        "được đăng ký sẵn.",
    "Extensions page:": "Trang tiện ích:",
    "Copy the address; paste it into the browser":
        "Sao chép địa chỉ rồi dán vào trình duyệt",
    "Copied:": "Đã sao chép:",
    "Registration": "Tình trạng đăng ký",
    "Browser": "Trình duyệt",
    "Start with Windows (in the tray)": "Chạy cùng Windows (thu nhỏ xuống khay)",
    # video / streaming
    "Video / stream": "Video / luồng phát",
    "Quality:": "Chất lượng:",
    "Preferred quality:": "Chất lượng ưu tiên:",
    "Best available": "Tốt nhất hiện có",
    "Audio only": "Chỉ lấy âm thanh",
    "Downloading video": "Đang tải video",
    "ffmpeg:": "ffmpeg:",
    "All files": "Tất cả tệp",
    "ffmpeg not found - videos are saved unmerged":
        "Không tìm thấy ffmpeg — video sẽ được lưu ở dạng chưa ghép",
    "yt-dlp not installed": "Chưa cài yt-dlp",
    "HLS playlist: segments download in parallel, then ffmpeg joins them.":
        "Playlist HLS: các đoạn được tải song song rồi ghép lại bằng ffmpeg.",
    "DASH manifest: yt-dlp picks the tracks, ffmpeg merges them.":
        "Manifest DASH: yt-dlp chọn luồng, ffmpeg ghép video và âm thanh.",
    "Video page: yt-dlp finds the streams, the download stays multi-segment.":
        "Trang video: yt-dlp tìm luồng tải, phần tải vẫn chia đoạn như thường.",
    # queues and the scheduler
    "Queues": "Hàng đợi",
    "Queue:": "Hàng đợi:",
    "No queue": "Không xếp hàng",
    "Move to queue": "Chuyển vào hàng đợi",
    "New queue": "Hàng đợi mới",
    "Rename": "Đổi tên",
    "Name:": "Tên:",
    "That name is already used.": "Tên này đã có rồi.",
    "Delete this queue? The downloads stay in the list.":
        "Xoá hàng đợi này? Các mục tải vẫn ở lại danh sách.",
    "Schedule": "Lịch chạy",
    "Start this queue automatically": "Tự động chạy hàng đợi này",
    "Start at:": "Bắt đầu lúc:",
    "Stop at:": "Dừng lúc:",
    "Days:": "Các ngày:",
    "Files at once:": "Số file cùng lúc:",
    "When finished:": "Khi xong:",
    "Next run:": "Lần chạy tới:",
    "not scheduled": "chưa đặt lịch",
    "Start now": "Chạy ngay",
    "Stop": "Dừng",
    "running": "đang chạy",
    "Do nothing": "Không làm gì",
    "Exit Boltdown": "Thoát Boltdown",
    "Shut down": "Tắt máy",
    "Hibernate": "Ngủ đông",
    "Sleep": "Ngủ",
    "Do it now": "Làm ngay",
    "Downloads finished - closing Boltdown":
        "Tải xong — sắp đóng Boltdown",
    "Downloads finished - shutting the computer down":
        "Tải xong — sắp tắt máy",
    "Downloads finished - hibernating": "Tải xong — sắp ngủ đông",
    "Downloads finished - going to sleep": "Tải xong — sắp vào chế độ ngủ",
    "Mon": "T2", "Tue": "T3", "Wed": "T4", "Thu": "T5",
    "Fri": "T6", "Sat": "T7", "Sun": "CN",
    # site grabber
    "Site Grabber": "Quét trang web",
    "Depth:": "Độ sâu:",
    "0 = only this page": "0 = chỉ trang này",
    "Files:": "Loại tệp:",
    "Extensions:": "Phần mở rộng:",
    "jpg, png, mp4 (empty = every file)": "jpg, png, mp4 (để trống = mọi tệp)",
    "URL must match:": "URL phải khớp:",
    "URL must not match:": "URL không được khớp:",
    "regular expression, optional": "biểu thức chính quy, không bắt buộc",
    "Page limit:": "Giới hạn số trang:",
    "Stay on the same host": "Chỉ đi trong cùng tên miền",
    "Scan": "Quét",
    "Scanning...": "Đang quét...",
    "Select all": "Chọn tất cả",
    "Select none": "Bỏ chọn tất cả",
    "Add selected": "Thêm mục đã chọn",
    "Found {n} files on {p} pages": "Thấy {n} tệp trên {p} trang",
    "limit reached": "đã chạm giới hạn",
    "Type": "Loại",
    "Everything": "Mọi thứ",
    "Images": "Ảnh",
    "Audio": "Âm thanh",
    "Archives": "Nén",
    # theme
    "Theme:": "Giao diện:",
    "Follow Windows": "Theo Windows",
    "Light": "Sáng",
    "Dark": "Tối",
    "Cyberpunk": "Cyberpunk",
    "Neon": "Neon",
    "Glass": "Kính mờ",
    "Nord": "Nord",
    "Dracula": "Dracula",
    # clipboard
    "Clipboard": "Clipboard",
    "Watch the clipboard": "Theo dõi clipboard",
    "Copy a link to download it": "Copy một link là app hỏi tải ngay",
    "Only text that is a bare link counts, so copying a paragraph does nothing.":
        "Chỉ tính khi nội dung copy đúng là một link trơ; copy cả đoạn văn thì bỏ qua.",
    # batch
    "Add many URLs": "Thêm hàng loạt URL",
    "One URL per line. [001-120] and [a-z] expand.":
        "Mỗi dòng một URL. Mẫu [001-120] và [a-z] sẽ được bung ra.",
    "{n} URLs ready": "Sẵn sàng {n} URL",
    "... and {n} more": "... và {n} mục nữa",
    "Add all": "Thêm tất cả",
    # history
    "History": "Lịch sử",
    "Download again": "Tải lại",
    "Clear history": "Xoá lịch sử",
    "Clear the whole history?": "Xoá toàn bộ lịch sử?",
    # checksum
    "Verify checksum": "Kiểm tra checksum",
    "File:": "Tệp:",
    "Algorithm:": "Thuật toán:",
    "Result:": "Kết quả:",
    "Expected:": "Giá trị mong đợi:",
    "not computed yet": "chưa tính",
    "paste the value from the download page": "dán giá trị trên trang tải về",
    "Compute": "Tính",
    "Copy": "Sao chép",
    "Match": "Khớp",
    "Does NOT match": "KHÔNG khớp",
    # drop box
    # proxy, cookies, categories
    "From browser": "Lấy từ trình duyệt",
    "Read the cookies this site set in Chrome/Edge":
        "Đọc cookie mà trang này đã lưu trong Chrome/Edge",
    "No Chromium browser profile was found.":
        "Không tìm thấy hồ sơ Chrome/Edge nào.",
    "That site has no cookies stored in your browser.":
        "Trình duyệt chưa lưu cookie nào của trang này.",
    "Use the Windows proxy settings": "Dùng thiết lập proxy của Windows",
    "Windows proxy": "Proxy của Windows",
    "System PAC file": "Tệp PAC của hệ thống",
    "Windows is set to connect directly": "Windows đang đặt kết nối thẳng",
    "socks5:// needs the socksio package": "socks5:// cần gói socksio",
    "Categories": "Danh mục",
    "One line per folder: Name = ext, ext, ext":
        "Mỗi dòng một thư mục: Tên = đuôi, đuôi, đuôi",
    "Restore defaults": "Khôi phục mặc định",
    "Unpack archives when they finish": "Tự giải nén khi tải xong",
    "Scan finished files with Defender": "Quét tệp bằng Defender sau khi tải",
    "Continue unfinished downloads at start": "Mở app là tải tiếp các mục dở",
    "Notify when a download finishes": "Báo khi tải xong",
    # statistics and bandwidth
    "Statistics": "Thống kê",
    "Files downloaded:": "Số tệp đã tải:",
    "Total size:": "Tổng dung lượng:",
    "Average file:": "Trung bình mỗi tệp:",
    "Per day:": "Mỗi ngày:",
    "Since:": "Từ ngày:",
    "Largest:": "Lớn nhất:",
    "Last 30 days": "30 ngày gần nhất",
    "No downloads yet": "Chưa có lượt tải nào",
    "Bandwidth window": "Khung giờ giới hạn",
    "Limit the speed between:": "Giới hạn tốc độ trong khoảng:",
    "Outside the window your normal limit comes back.":
        "Ngoài khung giờ này thì quay lại giới hạn thường ngày.",
    # after a download
    "Download finished": "Đã tải xong",
    "This URL is already in the list.": "URL này đã có trong danh sách.",
    "You downloaded this before.": "Bạn đã từng tải cái này.",
    "Add it again?": "Vẫn thêm lần nữa?",
    "Unpacked": "Đã giải nén",
    "Could not unpack": "Không giải nén được",
    "Defender": "Defender",
    # playlist
    "Playlist": "Danh sách phát",
    "List videos": "Liệt kê video",
    "Listing...": "Đang liệt kê...",
    "{n} videos": "{n} video",
    "That URL is a single video, not a playlist.":
        "URL này là một video lẻ, không phải danh sách phát.",
    "Uploader": "Kênh",
    # site rules
    "Site rules": "Quy tắc theo trang",
    "Host:": "Tên miền:",
    "Host": "Tên miền",
    "Enter a host": "Nhập tên miền",
    "Enabled": "Bật",
    "disabled": "đang tắt",
    "default": "mặc định",
    "New": "Tạo mới",
    "Note": "Ghi chú",
    "The narrowest match wins: cdn.example.com beats *.example.com beats *. "
    "Empty fields are left to the download itself.":
        "Khớp hẹp nhất thắng: cdn.example.com hơn *.example.com hơn *. "
        "Ô để trống thì giữ nguyên theo từng lượt tải.",
    "Drop box": "Hộp thả nổi",
    "Drop links here": "Thả link vào đây",
    "Hide drop box": "Ẩn hộp thả",
}

_TABLES = {"vi": _VI, "en": {}}

_current = "vi"


def set_language(code: str) -> None:
    global _current
    _current = code if code in _TABLES else "en"


def language() -> str:
    return _current


def tr(text: str) -> str:
    """Translate `text`; the English string is its own key."""
    return _TABLES.get(_current, {}).get(text, text)
