"""The request log must not hold what users search for."""

import logging

from app.main import _HideSearchText


def line(path):
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("10.0.0.1:1", "GET", path, "1.1", 200),
        None,
    )
    _HideSearchText().filter(record)
    return record.getMessage()


def test_the_search_text_is_left_out_of_access_log_lines():
    assert line("/api/search?q=%D8%A7%D9%84%D8%B5%D8%A8%D8%B1&page=1&limit=30") == (
        '10.0.0.1:1 - "GET /api/search?q=…&page=1&limit=30 HTTP/1.1" 200'
    )
    assert line("/api/search/meaning?limit=50&q=abc") == (
        '10.0.0.1:1 - "GET /api/search/meaning?limit=50&q=… HTTP/1.1" 200'
    )
    assert line("/api/works?q=x") == '10.0.0.1:1 - "GET /api/works?q=… HTTP/1.1" 200'
    assert (
        line("/api/books/4407/pages/52")
        == '10.0.0.1:1 - "GET /api/books/4407/pages/52 HTTP/1.1" 200'
    )
    assert line("/api/catalog/version?squid=1") == (
        '10.0.0.1:1 - "GET /api/catalog/version?squid=1 HTTP/1.1" 200'
    )  # only the value of q
