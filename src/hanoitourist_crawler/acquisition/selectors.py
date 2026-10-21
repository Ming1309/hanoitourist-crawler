"""Selectors verified against the scoped tour on 2026-10-05."""

ROOT = "article.node--type-tour[data-history-node-id]"
TITLE = "#block-blocktourtitle h3"
SECTIONS = ("diemnoibat", "lichtrinh", "lichkhoihanh", "baogom", "khongbaogom", "dieukien", "ghichu", "videos")
DAY_BUTTONS = "#lichtrinh .accordion-button"
SCHEDULE = "#lichkhoihanh"
DIALOG = '[role="dialog"]:has(.view-id-lich)'
DIALOG_LINK = ".link-xem-lich a.use-ajax"
SIDEBAR = ".lich-khoi-hanh-book-tour"
SCHEDULE_ID_PATTERN = r"^/tour/(\d+)/lich/(\d+)/booking/?$"

# Domestic listing HTTP/pager surveyed on 2026-10-06. Other views have
# independent pagers; offset ordering is not a completeness guarantee.
LISTING_ROOT = 'main .view-id-tour.view-display-id-page_trong_nuoc'
LISTING_ROWS = '.views-field-nothing'
LISTING_TITLE = '.tour-title h3 a'
LISTING_NEXT = '.pager a[rel=next]'
