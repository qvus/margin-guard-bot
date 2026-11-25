import json

import httpx
import pytest

from margin_guard.wildberries import (
    WBApiError,
    WBAuthenticationError,
    WBPermissionError,
    WBRateLimitError,
    WBResponseError,
    WildberriesClient,
)


def _response(request: httpx.Request, body, status_code: int = 200, headers=None):
    return httpx.Response(status_code, json=body, headers=headers, request=request)


def test_list_products_joins_cards_with_prices_and_paginates_prices():
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.host == "content-api.wildberries.ru":
            assert request.method == "POST"
            assert request.headers["Authorization"] == "read-only-token"
            assert json.loads(request.content)["settings"]["cursor"] == {"limit": 100}
            return _response(
                request,
                {"cards": [{"nmID": 12, "vendorCode": "SKU-12", "sizes": [{"chrtID": 55}]}]},
            )
        assert request.method == "GET"
        assert request.url.path == "/api/v2/list/goods/filter"
        if request.url.params["offset"] == "0":
            return _response(request, {"data": {"listGoods": [{"nmID": 12, "sizes": [{"price": 1200, "discountedPrice": 900}]}]}})
        return _response(request, {"data": {"listGoods": []}})

    client = WildberriesClient("read-only-token", transport=httpx.MockTransport(handler))
    try:
        products = client.list_products()
    finally:
        client.close()

    assert products == [
        {
            "card": {"nmID": 12, "vendorCode": "SKU-12", "sizes": [{"chrtID": 55}]},
            "pricing": {"nmID": 12, "sizes": [{"price": 1200, "discountedPrice": 900}]},
        }
    ]
    assert [call.url.params["offset"] for call in calls if call.url.host == "discounts-prices-api.wildberries.ru"] == ["0", "1000"]


def test_list_products_paginates_cards_using_documented_cursor():
    card_calls = 0

    def handler(request):
        nonlocal card_calls
        if request.url.host == "content-api.wildberries.ru":
            card_calls += 1
            payload = json.loads(request.content)
            cursor = payload["settings"]["cursor"]
            assert payload["settings"]["sort"] == {"ascending": True}
            assert payload["settings"]["filter"] == {"withPhoto": -1}
            if card_calls == 1:
                assert cursor == {"limit": 100}
                return _response(
                    request,
                    {
                        "cards": [{"nmID": i} for i in range(100)],
                        "cursor": {"total": 100, "updatedAt": "2026-01-01T00:00:00Z", "nmID": 99},
                    },
                )
            assert cursor == {"limit": 100, "updatedAt": "2026-01-01T00:00:00Z", "nmID": 99}
            return _response(request, {"cards": [{"nmID": 100}]})
        return _response(request, {"data": {"listGoods": []}})

    client = WildberriesClient("token", transport=httpx.MockTransport(handler))
    try:
        products = client.list_products()
    finally:
        client.close()

    assert card_calls == 2
    assert len(products) == 101
    assert products[-1] == {"card": {"nmID": 100}, "pricing": None}


def test_list_warehouses_uses_marketplace_read_endpoint():
    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/api/v3/warehouses"
        return _response(request, [{"id": 3, "name": "seller warehouse"}])

    client = WildberriesClient("token", transport=httpx.MockTransport(handler))
    try:
        assert client.list_warehouses() == [{"id": 3, "name": "seller warehouse"}]
    finally:
        client.close()


def test_get_stocks_posts_size_ids_to_read_only_endpoint():
    def handler(request):
        assert request.method == "POST"
        assert request.url.path == "/api/v3/stocks/42"
        assert json.loads(request.content) == {"chrtIds": [5, 6]}
        return _response(request, {"stocks": [{"chrtId": 5, "amount": 7, "warehouseId": 42}]})

    client = WildberriesClient("token", transport=httpx.MockTransport(handler))
    try:
        assert client.get_stocks(42, [5, 6]) == [{"chrtId": 5, "amount": 7, "warehouseId": 42}]
    finally:
        client.close()


def test_get_stocks_empty_ids_do_not_make_a_request():
    client = WildberriesClient(
        "token",
        transport=httpx.MockTransport(lambda request: pytest.fail("unexpected request")),
    )
    try:
        assert client.get_stocks(42, []) == []
    finally:
        client.close()


def test_retries_rate_limit_and_honors_retry_after():
    attempts = 0
    delays = []

    def handler(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return _response(request, {}, 429, {"Retry-After": "2"})
        return _response(request, [])

    client = WildberriesClient(
        "token", max_retries=1, retry_max_delay=3, sleep=delays.append,
        transport=httpx.MockTransport(handler),
    )
    try:
        assert client.list_warehouses() == []
    finally:
        client.close()
    assert attempts == 2
    assert delays == [2.0]


def test_rate_limit_exhaustion_is_reported_without_response_body():
    def handler(request):
        return _response(request, {"secret": "must not leak"}, 429)

    client = WildberriesClient(
        "top-secret-token", max_retries=1, retry_base_delay=0,
        transport=httpx.MockTransport(handler), sleep=lambda _: None,
    )
    try:
        with pytest.raises(WBRateLimitError) as error:
            client.list_warehouses()
    finally:
        client.close()
    assert "must not leak" not in str(error.value)
    assert "top-secret-token" not in str(error.value)


def test_retries_timeout_with_exponential_delay():
    attempts = 0
    delays = []

    def handler(request):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise httpx.ReadTimeout("private timeout details")
        return _response(request, [])

    client = WildberriesClient(
        "token", max_retries=2, retry_base_delay=0.25, retry_max_delay=1,
        sleep=delays.append, transport=httpx.MockTransport(handler),
    )
    try:
        assert client.list_warehouses() == []
    finally:
        client.close()
    assert attempts == 3
    assert delays == [0.25, 0.5]


@pytest.mark.parametrize(
    ("status", "error_type", "expected_message"),
    [
        (401, WBAuthenticationError, "token"),
        (403, WBPermissionError, "permission"),
        (500, WBApiError, "HTTP 500"),
    ],
)
def test_http_errors_are_safe_and_actionable(status, error_type, expected_message):
    def handler(request):
        return _response(request, {"token": "secret response payload"}, status)

    client = WildberriesClient("secret-token", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(error_type) as error:
            client.list_warehouses()
    finally:
        client.close()
    assert expected_message in str(error.value)
    assert "secret response payload" not in str(error.value)
    assert "secret-token" not in str(error.value)


def test_invalid_json_response_raises_sanitized_error():
    def handler(request):
        return httpx.Response(200, content=b"not-json", request=request)

    client = WildberriesClient("token", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(WBResponseError, match="invalid JSON"):
            client.list_warehouses()
    finally:
        client.close()


def test_invalid_response_shape_raises_error():
    client = WildberriesClient(
        "token", transport=httpx.MockTransport(lambda request: _response(request, {"items": []}))
    )
    try:
        with pytest.raises(WBResponseError, match="warehouse list"):
            client.list_warehouses()
    finally:
        client.close()


@pytest.mark.parametrize(
    ("warehouse_id", "chrt_ids", "message"),
    [(0, [1], "warehouse_id"), (1, [0], "chrt_ids"), (1, list(range(1001)), "1,000")],
)
def test_stock_request_validation(warehouse_id, chrt_ids, message):
    client = WildberriesClient(
        "token", transport=httpx.MockTransport(lambda request: pytest.fail("unexpected request"))
    )
    try:
        with pytest.raises(ValueError, match=message):
            client.get_stocks(warehouse_id, chrt_ids)
    finally:
        client.close()


def test_rejects_empty_token():
    with pytest.raises(ValueError, match="must not be empty"):
        WildberriesClient("  ")


def test_internal_transport_rejects_write_methods():
    client = WildberriesClient(
        "token", transport=httpx.MockTransport(lambda request: pytest.fail("unexpected request"))
    )
    try:
        with pytest.raises(ValueError, match="read-only"):
            client._request_json("PUT", "https://marketplace-api.wildberries.ru", "/api/v3/stocks/1")
    finally:
        client.close()
