"""Hardened read-only Wildberries Seller API HTTP client and gateway adapter."""

from __future__ import annotations

import json as jsonlib
import re
import time
from collections.abc import Callable, Sequence
from decimal import Decimal, InvalidOperation
from threading import BoundedSemaphore
from typing import Any

import httpx

from margin_guard.domain.entities import CatalogProduct, ProductSizePrice
from margin_guard.domain.exceptions import (
    DomainValidationError,
    MarketplaceAuthError,
    MarketplacePermissionError,
    MarketplaceRateLimitError,
    MarketplaceResponseError,
    MarketplaceUnavailableError,
)
from margin_guard.domain.value_objects import validate_marketplace_token

CONTENT_API = "https://content-api.wildberries.ru"
PRICES_API = "https://discounts-prices-api.wildberries.ru"
MARKETPLACE_API = "https://marketplace-api.wildberries.ru"

DEFAULT_MAX_PAGES = 200
DEFAULT_MAX_RESPONSE_BYTES = 10 * 1024 * 1024  # 10 MiB

_ALLOWED_STATIC_ENDPOINTS: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("POST", CONTENT_API, "/content/v2/get/cards/list"),
        ("GET", PRICES_API, "/api/v2/list/goods/filter"),
        ("GET", MARKETPLACE_API, "/api/v3/warehouses"),
    }
)
_STOCK_PATH_RE = re.compile(r"/api/v3/stocks/[1-9][0-9]{0,17}")


class _RedactedSecret:
    """Prevent accidental credential exposure in debuggers, reprs, or locals dumps."""

    __slots__ = ("_secret",)

    def __init__(self, secret: str) -> None:
        self._secret = secret

    def reveal(self) -> str:
        return self._secret

    def __repr__(self) -> str:
        return "<REDACTED_WB_TOKEN>"

    def __str__(self) -> str:
        return "<REDACTED_WB_TOKEN>"


class WildberriesClient:
    """Hardened read-only client for the official Wildberries Seller API.

    Security controls:
    - Strict printable-ASCII token validation blocking HTTP header injection and
      ``UnicodeEncodeError`` crashes.
    - Explicit read-only endpoint allowlist; mutating methods are structurally blocked.
    - ``follow_redirects=False`` so an ``Authorization`` header is never forwarded
      across HTTP redirects.
    - ``trust_env=False`` preventing ambient proxy/cert hijacking unless explicitly configured.
    - Streaming byte cap (``max_response_bytes``) preventing oversized JSON memory bombs.
    - Bounded pagination (``max_pages``) preventing infinite cursor/offset DoS loops.
    - Sanitized domain exceptions omitting response bodies and credentials.
    """

    def __init__(
        self,
        token: str,
        *,
        timeout: float = 10.0,
        max_retries: int = 3,
        retry_base_delay: float = 0.5,
        retry_max_delay: float = 8.0,
        max_concurrent_requests: int = 4,
        max_pages: int = DEFAULT_MAX_PAGES,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        try:
            validated_token = validate_marketplace_token(token)
        except DomainValidationError as exc:
            raise MarketplaceAuthError(
                "WB API token format is invalid"
            ) from exc

        if max_retries < 0:
            raise ValueError("max_retries must be zero or greater")
        if max_concurrent_requests < 1:
            raise ValueError("max_concurrent_requests must be at least one")
        if max_pages < 1:
            raise ValueError("max_pages must be at least one")
        if max_response_bytes < 1024:
            raise ValueError("max_response_bytes must be at least 1024")

        self._secret = _RedactedSecret(validated_token)
        self._max_retries = max_retries
        self._retry_base_delay = max(0.0, retry_base_delay)
        self._retry_max_delay = max(self._retry_base_delay, retry_max_delay)
        self._max_pages = max_pages
        self._max_response_bytes = max_response_bytes
        self._sleep = sleep
        self._semaphore = BoundedSemaphore(max_concurrent_requests)
        self._http = httpx.Client(
            timeout=httpx.Timeout(timeout),
            transport=transport,
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(
                max_connections=max_concurrent_requests * 2,
                max_keepalive_connections=max_concurrent_requests,
            ),
            headers={
                "Authorization": self._secret.reveal(),
                "Accept": "application/json",
            },
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> WildberriesClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def list_products(self) -> list[dict[str, Any]]:
        """Fetch product cards joined with current size-level discount prices."""
        cards = self._list_product_cards()
        prices = self._list_product_prices()
        prices_by_id = {
            item["nmID"]: item
            for item in prices
            if isinstance(item, dict) and "nmID" in item
        }
        products: list[dict[str, Any]] = []
        for card in cards:
            if not isinstance(card, dict):
                raise MarketplaceResponseError(
                    "Wildberries returned an invalid product card"
                )
            nm_id = card.get("nmID")
            products.append({"card": card, "pricing": prices_by_id.get(nm_id)})
        return products

    def list_warehouses(self) -> list[dict[str, Any]]:
        """Fetch seller FBS warehouses via read-only Marketplace API."""
        result = self._request_json("GET", MARKETPLACE_API, "/api/v3/warehouses")
        if not isinstance(result, list) or any(
            not isinstance(row, dict) for row in result
        ):
            raise MarketplaceResponseError(
                "Wildberries returned an invalid warehouse list"
            )
        return result

    def get_stocks(
        self, warehouse_id: int, chrt_ids: Sequence[int]
    ) -> list[dict[str, Any]]:
        """Read stock amounts for up to 1,000 size IDs at a seller warehouse."""
        if warehouse_id < 1:
            raise ValueError("warehouse_id must be a positive integer")
        if len(chrt_ids) > 1000:
            raise ValueError("Wildberries accepts at most 1,000 chrtIds per request")
        if any(chrt_id < 1 for chrt_id in chrt_ids):
            raise ValueError("chrt_ids must contain positive integers")
        if not chrt_ids:
            return []
        result = self._request_json(
            "POST",
            MARKETPLACE_API,
            f"/api/v3/stocks/{warehouse_id}",
            json_body={"chrtIds": list(chrt_ids)},
        )
        if not isinstance(result, dict) or not isinstance(result.get("stocks"), list):
            raise MarketplaceResponseError(
                "Wildberries returned an invalid stock response"
            )
        stocks: list[Any] = result["stocks"]
        if any(not isinstance(row, dict) for row in stocks):
            raise MarketplaceResponseError(
                "Wildberries returned an invalid stock record"
            )
        return stocks

    def _list_product_cards(self) -> list[dict[str, Any]]:
        cards: list[dict[str, Any]] = []
        cursor: dict[str, Any] = {"limit": 100}
        for _ in range(self._max_pages):
            payload = {
                "settings": {
                    "sort": {"ascending": True},
                    "cursor": cursor,
                    "filter": {"withPhoto": -1},
                }
            }
            result = self._request_json(
                "POST",
                CONTENT_API,
                "/content/v2/get/cards/list",
                json_body=payload,
            )
            if not isinstance(result, dict) or not isinstance(
                result.get("cards"), list
            ):
                raise MarketplaceResponseError(
                    "Wildberries returned an invalid product-card response"
                )
            page: list[Any] = result["cards"]
            if any(not isinstance(card, dict) for card in page):
                raise MarketplaceResponseError(
                    "Wildberries returned an invalid product card"
                )
            cards.extend(page)
            response_cursor = result.get("cursor") or {}
            if not isinstance(response_cursor, dict):
                raise MarketplaceResponseError(
                    "Wildberries returned an invalid pagination cursor"
                )
            total = response_cursor.get("total", len(page))
            if len(page) < 100 or (isinstance(total, int) and total < 100):
                return cards

            updated_at = response_cursor.get("updatedAt")
            nm_id = response_cursor.get("nmID")
            if not updated_at or nm_id is None:
                raise MarketplaceResponseError(
                    "Wildberries omitted the product pagination cursor"
                )
            next_cursor = {"limit": 100, "updatedAt": updated_at, "nmID": nm_id}
            if next_cursor == cursor:
                raise MarketplaceResponseError(
                    "Wildberries repeated the product pagination cursor"
                )
            cursor = next_cursor

        raise MarketplaceResponseError(
            "Wildberries product-card pagination exceeded safety page limit"
        )

    def _list_product_prices(self) -> list[dict[str, Any]]:
        prices: list[dict[str, Any]] = []
        offset = 0
        for _ in range(self._max_pages):
            result = self._request_json(
                "GET",
                PRICES_API,
                "/api/v2/list/goods/filter",
                params={"limit": 1000, "offset": offset},
            )
            data = result.get("data") if isinstance(result, dict) else None
            page = data.get("listGoods") if isinstance(data, dict) else None
            if not isinstance(page, list) or any(
                not isinstance(row, dict) for row in page
            ):
                raise MarketplaceResponseError(
                    "Wildberries returned an invalid product-price response"
                )
            prices.extend(page)
            if not page:
                return prices
            offset += 1000

        raise MarketplaceResponseError(
            "Wildberries product-price pagination exceeded safety page limit"
        )

    def _request_json(
        self,
        method: str,
        base_url: str,
        path: str,
        *,
        params: MappingLike | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        is_stock_lookup = (
            method == "POST"
            and base_url == MARKETPLACE_API
            and _STOCK_PATH_RE.fullmatch(path) is not None
        )
        if (method, base_url, path) not in _ALLOWED_STATIC_ENDPOINTS and not is_stock_lookup:
            raise ValueError(
                "This client only allows the configured read-only WB methods"
            )

        url = f"{base_url}{path}"
        for attempt in range(self._max_retries + 1):
            try:
                with self._semaphore, self._http.stream(
                    method, url, params=params, json=json_body
                ) as response:
                    status = response.status_code
                    retry_delay = (
                        self._retry_after(response, attempt) if status == 429 else 0.0
                    )
                    raw_body = (
                        self._read_bounded_body(response)
                        if 200 <= status < 300
                        else b""
                    )
            except httpx.TimeoutException as exc:
                if attempt >= self._max_retries:
                    raise MarketplaceUnavailableError(
                        "Wildberries request timed out after retries"
                    ) from exc
                self._sleep(self._backoff(attempt))
                continue
            except httpx.RequestError as exc:
                raise MarketplaceUnavailableError(
                    "Could not connect to the Wildberries API"
                ) from exc

            if status == 429:
                if attempt >= self._max_retries:
                    raise MarketplaceRateLimitError(
                        "Wildberries rate limit persisted after retries"
                    )
                self._sleep(retry_delay)
                continue
            if status == 401:
                raise MarketplaceAuthError("Wildberries rejected the API token (401)")
            if status == 403:
                raise MarketplacePermissionError(
                    "The WB API token lacks permission for this request (403)"
                )
            if 300 <= status < 400:
                raise MarketplaceUnavailableError(
                    f"Wildberries returned an unexpected redirect (HTTP {status})"
                )
            if status in (502, 503, 504) and attempt < self._max_retries:
                self._sleep(self._backoff(attempt))
                continue
            if not (200 <= status < 300):
                raise MarketplaceUnavailableError(
                    f"Wildberries API request failed (HTTP {status})"
                )

            try:
                return jsonlib.loads(raw_body)
            except (ValueError, TypeError) as exc:
                raise MarketplaceResponseError(
                    "Wildberries returned invalid JSON"
                ) from exc

        raise MarketplaceUnavailableError("Wildberries retries exhausted")

    def _read_bounded_body(self, response: httpx.Response) -> bytes:
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                if int(content_length) > self._max_response_bytes:
                    raise MarketplaceResponseError(
                        "Wildberries response exceeded maximum allowed size"
                    )
            except ValueError:
                pass

        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > self._max_response_bytes:
                raise MarketplaceResponseError(
                    "Wildberries response exceeded maximum allowed size"
                )
            chunks.append(chunk)
        return b"".join(chunks)

    def _backoff(self, attempt: int) -> float:
        return float(min(self._retry_base_delay * (2**attempt), self._retry_max_delay))

    def _retry_after(self, response: httpx.Response, attempt: int) -> float:
        value = response.headers.get("Retry-After")
        if value:
            try:
                return float(min(max(0.0, float(value)), self._retry_max_delay))
            except ValueError:
                pass
        return self._backoff(attempt)


MappingLike = dict[str, str | int]


def _parse_api_decimal(raw: object) -> Decimal | None:
    if raw is None or isinstance(raw, bool):
        return None
    text = str(raw).strip()
    if not text or len(text) > 32:
        return None
    try:
        val = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return val if val.is_finite() else None


def map_raw_wb_product(raw: dict[str, Any]) -> CatalogProduct | None:
    """Convert a raw Wildberries card+pricing dict into a clean ``CatalogProduct``."""
    card = raw.get("card")
    if not isinstance(card, dict) or card.get("nmID") is None:
        return None
    raw_nm_id = str(card["nmID"]).strip()
    if not raw_nm_id.isdigit():
        return None

    vendor_code = (
        str(card["vendorCode"]) if card.get("vendorCode") is not None else None
    )
    title = str(card["title"]) if card.get("title") is not None else None

    pricing = raw.get("pricing")
    raw_sizes = pricing.get("sizes") if isinstance(pricing, dict) else None
    sizes: list[ProductSizePrice] = []
    if isinstance(raw_sizes, list):
        for size in raw_sizes[:50]:
            if not isinstance(size, dict):
                continue
            raw_discount = size.get("discount")
            discount_pct = (
                int(raw_discount)
                if isinstance(raw_discount, int) and not isinstance(raw_discount, bool)
                else None
            )
            raw_chrt = size.get("chrtID")
            chrt_id = (
                int(raw_chrt)
                if isinstance(raw_chrt, int) and not isinstance(raw_chrt, bool)
                else None
            )
            sizes.append(
                ProductSizePrice(
                    price=_parse_api_decimal(size.get("price")),
                    discounted_price=_parse_api_decimal(size.get("discountedPrice")),
                    discount_percent=discount_pct,
                    chrt_id=chrt_id,
                )
            )

    return CatalogProduct(
        nm_id=raw_nm_id,
        vendor_code=vendor_code,
        title=title,
        sizes=tuple(sizes),
    )


class WildberriesGateway:
    """MarketplaceGatewayPort adapter translating raw WB responses into Domain Entities."""

    def __init__(
        self,
        *,
        timeout: float = 10.0,
        max_retries: int = 3,
        max_pages: int = DEFAULT_MAX_PAGES,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._timeout = timeout
        self._max_retries = max_retries
        self._max_pages = max_pages
        self._max_response_bytes = max_response_bytes
        self._transport = transport

    def fetch_catalog_products(self, token: str) -> Sequence[CatalogProduct]:
        with WildberriesClient(
            token,
            timeout=self._timeout,
            max_retries=self._max_retries,
            max_pages=self._max_pages,
            max_response_bytes=self._max_response_bytes,
            transport=self._transport,
        ) as client:
            raw_products = client.list_products()

        mapped: list[CatalogProduct] = []
        for item in raw_products:
            product = map_raw_wb_product(item)
            if product is not None:
                mapped.append(product)
        return mapped
