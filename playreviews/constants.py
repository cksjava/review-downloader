"""Values the Play Store web client sends with a review page request."""

# The review RPC is oCPfdb. Its sort field is 1, 2, or 3.
SORTS = {
    "relevant": 1,
    "newest": 2,
    "rating": 3,
}

# Device filter inside the same RPC. Omit it to collect every device.
DEVICES = {
    "mobile": 2,
    "tablet": 3,
    "chromebook": 5,
    "tv": 6,
}

DEVICE_LABELS = {
    2: "mobile",
    3: "tablet",
    5: "chromebook",
    6: "tv",
}

MOBILE_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.6778.200 Mobile Safari/537.36"
)

# A phone loads about this many reviews each time the next page is requested.
DEFAULT_PAGE_SIZE = 40
MAX_PAGE_SIZE = 100
DEFAULT_DELAY_SECONDS = 2.5
