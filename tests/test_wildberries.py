"""Tests for the hardened read-only Wildberries HTTP client and gateway."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx
import pytest

from margin_guard.domain.exceptions import (
    MarketplaceAuthError,
    MarketplaceError,
    MarketplacePermissionError,
    MarketplaceRateLimitError,
    MarketplaceResponseError,
    MarketplaceUnavailableError,
)
from margin_guard.infrastructure.marketplace.wildberries_client import (
    WildberriesClient,
    WildberriesGateway,
)


def _json_response(
    request: httpx.Request,
    body: Any,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return httpx.Response(status_code, json=body, headers=headers, request=request)


def test_gateway_joins_cards_with_prices_and_maps_domain_entities() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "content-api.wildberries.ru":
            assert request.method == "POST"
            assert request.headers["Authorization"] == "read-only-token"
            assert json.loads(request.content)["settings"]["cursor"] == {"limit": 100}
            return _json_response(
                request,
                {
                    "cards": [
                        {
                            "nmID": 12,
                            "vendorCode": "SKU-12",
                            "title": "Jacket",
                            "sizes": [{"chrtID": 55}],
                        }
                    ]
                },
            )
        assert request.method == "GET"
        assert request.url.path == "/api/v2/list/goods/filter"
        if request.url.params["offset"] == "0":
            return _json_response(
                request,
                {
                    "data": {
                        "listGoods": [
                            {
                                "nmID": 12,
                                "sizes": [
                                    {"price": 1200, "discountedPrice": 900, "discount": 25}
                                ],
                            }
                        ]
                    }
                },
            )
        return _json_response(request, {"data": {"listGoods": []}})

    gateway = WildberriesGateway(transport=httpx.MockTransport(handler))
    products = gateway.fetch_catalog_products("read-only-token")

    assert len(products) == 1
    assert products[0].nm_id == "12"
    assert products[0].vendor_code == "SKU-12"
    assert products[0].sizes[0].discounted_price == Decimal("900")
    assert [
        c.url.params["offset"]
        for c in calls
        if c.url.host == "discounts-prices-api.wildberries.ru"
    ] == ["0", "1000"]


def test_list_products_paginates_cards_with_cursor_and_enforces_max_pages() -> None:
    card_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal card_calls
        if request.url.host == "content-api.wildberries.ru":
            card_calls += 1
            if card_calls == 1:
                return _json_response(
                    request,
                    {
                        "cards": [{"nmID": i} for i in range(100)],
                        "cursor": {
                            "total": 100,
                            "updatedAt": "2026-01-01T00:00:00Z",
                            "nmID": 99,
                        },
                    },
                )
            return _json_response(request, {"cards": [{"nmID": 100}]})
        return _json_response(request, {"data": {"listGoods": []}})

    with WildberriesClient("token", transport=httpx.MockTransport(handler)) as client:
        products = client.list_products()

    assert card_calls == 2
    assert len(products) == 101

    # Verify max_pages DoS protection when upstream loops endlessly
    def infinite_handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            request,
            {
                "cards": [{"nmID": i} for i in range(100)],
                "cursor": {
                    "total": 100,
                    "updatedAt": f"2026-01-01T00:00:{request.headers.get('x', '00')}Z",
                    "nmID": id(request),
                },
            },
        )

    with (
        WildberriesClient(
            "token", max_pages=2, transport=httpx.MockTransport(infinite_handler)
        ) as bounded_client,
        pytest.raises(MarketplaceResponseError, match="page limit"),
    ):
        bounded_client.list_products()


def test_warehouses_and_stocks_read_only_endpoints_and_validation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/warehouses":
            return _json_response(request, [{"id": 3, "name": "Main"}])
        assert request.url.path == "/api/v3/stocks/42"
        assert json.loads(request.content) == {"chrtIds": [5, 6]}
        return _json_response(
            request, {"stocks": [{"chrtId": 5, "amount": 7, "warehouseId": 42}]}
        )

    with WildberriesClient("token", transport=httpx.MockTransport(handler)) as client:
        assert client.list_warehouses() == [{"id": 3, "name": "Main"}]
        assert client.get_stocks(42, [5, 6]) == [
            {"chrtId": 5, "amount": 7, "warehouseId": 42}
        ]
        assert client.get_stocks(42, []) == []

        with pytest.raises(ValueError, match="warehouse_id"):
            client.get_stocks(0, [1])
        with pytest.raises(ValueError, match="1,000"):
            client.get_stocks(1, list(range(1, 1002)))
        with pytest.raises(ValueError, match="read-only"):
            client._request_json(
                "PUT", "https://marketplace-api.wildberries.ru", "/api/v3/stocks/1"
            )


def test_non_ascii_token_raises_domain_auth_error_instead_of_unicode_crash() -> None:
    with pytest.raises(MarketplaceAuthError):
        WildberriesClient("некорректный-кириллический-токен")


def test_redirects_are_not_followed_and_oversized_responses_are_blocked() -> None:
    def redirect_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"Location": "https://attacker.example/steal"},
            request=request,
        )

    with (
        WildberriesClient(
            "secret-wb-token", transport=httpx.MockTransport(redirect_handler)
        ) as client,
        pytest.raises(MarketplaceUnavailableError, match="redirect"),
    ):
        client.list_warehouses()

    def giant_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"[" + (b"1," * 2048) + b"1]", request=request)

    with (
        WildberriesClient(
            "secret-wb-token",
            max_response_bytes=1024,
            transport=httpx.MockTransport(giant_handler),
        ) as client,
        pytest.raises(MarketplaceResponseError, match="maximum allowed size"),
    ):
        client.list_warehouses()


def test_retries_rate_limits_timeouts_and_sanitizes_http_errors() -> None:
    attempts = 0
    delays: list[float] = []

    def flaky_handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return _json_response(request, {}, 429, {"Retry-After": "1.5"})
        if attempts == 2:
            raise httpx.ReadTimeout("internal socket detail")
        return _json_response(request, [])

    with WildberriesClient(
        "secret-token",
        max_retries=3,
        retry_base_delay=0.25,
        retry_max_delay=2.0,
        sleep=delays.append,
        transport=httpx.MockTransport(flaky_handler),
    ) as client:
        assert repr(client._secret) == "<REDACTED_WB_TOKEN>"
        assert client.list_warehouses() == []

    assert attempts == 3
    assert delays == [1.5, 0.5]


@pytest.mark.parametrize(
    ("status", "exc_cls"),
    [
        (401, MarketplaceAuthError),
        (403, MarketplacePermissionError),
        (429, MarketplaceRateLimitError),
        (500, MarketplaceError),
    ],
)
def test_error_responses_never_leak_payload_or_token(
    status: int, exc_cls: type[Exception]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(request, {"leak": "sensitive-body"}, status)

    with WildberriesClient(
        "my-secret-wb-token",
        max_retries=0,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(exc_cls) as exc_info:
            client.list_warehouses()
        assert "sensitive-body" not in str(exc_info.value)
        assert "my-secret-wb-token" not in str(exc_info.value)


def test_503_retry_invalid_json_network_error_and_malformed_shapes() -> None:
    calls = 0

    def transient_503(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, content=b"unavailable", request=request)
        return _json_response(request, [])

    with WildberriesClient(
        "token",
        max_retries=1,
        retry_base_delay=0.0,
        sleep=lambda _: None,
        transport=httpx.MockTransport(transient_503),
    ) as client:
        assert str(client._secret) == "<REDACTED_WB_TOKEN>"
        assert client.list_warehouses() == []

    with (
        WildberriesClient(
            "token",
            transport=httpx.MockTransport(
                lambda req: httpx.Response(200, content=b"not-json", request=req)
            ),
        ) as bad_json_client,
        pytest.raises(MarketplaceResponseError, match="invalid JSON"),
    ):
        bad_json_client.list_warehouses()

    def net_fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns fail", request=request)

    with (
        WildberriesClient("token", transport=httpx.MockTransport(net_fail)) as net_client,
        pytest.raises(MarketplaceUnavailableError, match="connect"),
    ):
        net_client.list_warehouses()

    with (
        WildberriesClient(
            "token",
            transport=httpx.MockTransport(
                lambda req: _json_response(req, {"wrong": "shape"})
            ),
        ) as shape_client,
        pytest.raises(MarketplaceResponseError, match="warehouse list"),
    ):
        shape_client.list_warehouses()
