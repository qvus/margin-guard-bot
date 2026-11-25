"""Read-only client for the official Wildberries seller API.

Only the documented methods for product cards, prices, seller warehouses and
seller-warehouse stocks are exposed. In particular, this module has no methods
for changing prices, cards, warehouses or stock.
"""

from __future__ import annotations

import re
import time
from threading import BoundedSemaphore
from typing import Any

import httpx


CONTENT_API = "https://content-api.wildberries.ru"
PRICES_API = "https://discounts-prices-api.wildberries.ru"
MARKETPLACE_API = "https://marketplace-api.wildberries.ru"


class WBApiError(RuntimeError):
    """A sanitized API failure; response content and credentials are omitted."""


class WBAuthenticationError(WBApiError):
    """The WB token is missing, invalid or expired (HTTP 401)."""


class WBPermissionError(WBApiError):
    """The token does not have permission for the requested API category."""


class WBRateLimitError(WBApiError):
    """The API continued returning HTTP 429 after the configured retries."""


class WBResponseError(WBApiError):
    """The API returned a malformed or unexpected response."""


class WildberriesClient:
    """Fetch products, prices and seller FBS stock using a read-only token."""

    def __init__(
        self,
        token: str,
        *,
        timeout: float = 10.0,
        max_retries: int = 3,
        retry_base_delay: float = 0.5,
        retry_max_delay: float = 8.0,
        max_concurrent_requests: int = 4,
        transport: httpx.BaseTransport | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        if not token or not token.strip():
            raise ValueError("WB API token must not be empty")
        if max_retries < 0:
            raise ValueError("max_retries must be zero or greater")
        if max_concurrent_requests < 1:
            raise ValueError("max_concurrent_requests must be at least one")
        self._token = token.strip()
        self._max_retries = max_retries
        self._retry_base_delay = max(0.0, retry_base_delay)
        self._retry_max_delay = max(self._retry_base_delay, retry_max_delay)
        self._sleep = sleep
        self._semaphore = BoundedSemaphore(max_concurrent_requests)
        self._http = httpx.Client(
            timeout=timeout,
            transport=transport,
            headers={"Authorization": self._token},
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> WildberriesClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def list_products(self) -> list[dict[str, Any]]:
        """Return product cards joined with their current price records.

        Card pagination follows the official cursor contract. Prices are read
        independently using offset pagination and joined by WB ``nmID``. The
        original API records are retained under ``card`` and ``pricing``.
        """
        cards = self._list_product_cards()
        prices = self._list_product_prices()
        prices_by_id = {
            item["nmID"]: item
            for item in prices
            if isinstance(item, dict) and "nmID" in item
        }
        products = []
        for card in cards:
            if not isinstance(card, dict):
                raise WBResponseError("Wildberries returned an invalid product card")
            nm_id = card.get("nmID")
            products.append({"card": card, "pricing": prices_by_id.get(nm_id)})
        return products

    def list_warehouses(self) -> list[dict[str, Any]]:
        """Return the seller's warehouses used by the Marketplace API."""
        result = self._request_json("GET", MARKETPLACE_API, "/api/v3/warehouses")
        if not isinstance(result, list) or any(not isinstance(row, dict) for row in result):
            raise WBResponseError("Wildberries returned an invalid warehouse list")
        return result

    def get_stocks(self, warehouse_id: int, chrt_ids: list[int]) -> list[dict[str, Any]]:
        """Read stock for up to 1,000 size IDs at one seller warehouse."""
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
            json={"chrtIds": chrt_ids},
        )
        if not isinstance(result, dict) or not isinstance(result.get("stocks"), list):
            raise WBResponseError("Wildberries returned an invalid stock response")
        if any(not isinstance(row, dict) for row in result["stocks"]):
            raise WBResponseError("Wildberries returned an invalid stock record")
        return result["stocks"]

    def _list_product_cards(self) -> list[dict[str, Any]]:
        cards: list[dict[str, Any]] = []
        cursor: dict[str, Any] = {"limit": 100}
        while True:
            payload = {
                "settings": {
                    "sort": {"ascending": True},
                    "cursor": cursor,
                    "filter": {"withPhoto": -1},
                }
            }
            result = self._request_json(
                "POST", CONTENT_API, "/content/v2/get/cards/list", json=payload
            )
            if not isinstance(result, dict) or not isinstance(result.get("cards"), list):
                raise WBResponseError("Wildberries returned an invalid product-card response")
            page = result["cards"]
            if any(not isinstance(card, dict) for card in page):
                raise WBResponseError("Wildberries returned an invalid product card")
            cards.extend(page)
            response_cursor = result.get("cursor") or {}
            total = response_cursor.get("total", len(page))
            if len(page) < 100 or total < 100:
                break
            updated_at = response_cursor.get("updatedAt")
            nm_id = response_cursor.get("nmID")
            if not updated_at or nm_id is None:
                raise WBResponseError("Wildberries omitted the product pagination cursor")
            next_cursor = {"limit": 100, "updatedAt": updated_at, "nmID": nm_id}
            if next_cursor == cursor:
                raise WBResponseError("Wildberries repeated the product pagination cursor")
            cursor = next_cursor
        return cards

    def _list_product_prices(self) -> list[dict[str, Any]]:
        prices: list[dict[str, Any]] = []
        offset = 0
        while True:
            result = self._request_json(
                "GET",
                PRICES_API,
                "/api/v2/list/goods/filter",
                params={"limit": 1000, "offset": offset},
            )
            data = result.get("data") if isinstance(result, dict) else None
            page = data.get("listGoods") if isinstance(data, dict) else None
            if not isinstance(page, list) or any(not isinstance(row, dict) for row in page):
                raise WBResponseError("Wildberries returned an invalid product-price response")
            prices.extend(page)
            if not page:
                break
            offset += 1000
        return prices

    def _request_json(
        self,
        method: str,
        base_url: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        # The WB card-list and stock lookup methods use POST for reads. Keep an
        # explicit allowlist so this shared transport cannot send write calls.
        allowed_reads = {
            ("POST", CONTENT_API, "/content/v2/get/cards/list"),
            ("GET", PRICES_API, "/api/v2/list/goods/filter"),
            ("GET", MARKETPLACE_API, "/api/v3/warehouses"),
        }
        is_stock_lookup = (
            method == "POST"
            and base_url == MARKETPLACE_API
            and re.fullmatch(r"/api/v3/stocks/[1-9][0-9]*", path) is not None
        )
        if (method, base_url, path) not in allowed_reads and not is_stock_lookup:
            raise ValueError("This client only allows the configured read-only WB methods")
        url = f"{base_url}{path}"
        for attempt in range(self._max_retries + 1):
            try:
                with self._semaphore:
                    response = self._http.request(
                        method, url, params=params, json=json
                    )
            except httpx.TimeoutException as exc:
                if attempt >= self._max_retries:
                    raise WBApiError("Wildberries request timed out after retries") from exc
                self._sleep(self._backoff(attempt))
                continue
            except httpx.RequestError as exc:
                raise WBApiError("Could not connect to the Wildberries API") from exc

            if response.status_code == 429:
                if attempt >= self._max_retries:
                    raise WBRateLimitError("Wildberries rate limit persisted after retries")
                self._sleep(self._retry_after(response, attempt))
                continue
            if response.status_code == 401:
                raise WBAuthenticationError("Wildberries rejected the API token (401)")
            if response.status_code == 403:
                raise WBPermissionError("The WB API token lacks permission for this request (403)")
            if not response.is_success:
                raise WBApiError(f"Wildberries API request failed (HTTP {response.status_code})")
            try:
                return response.json()
            except (ValueError, TypeError) as exc:
                raise WBResponseError("Wildberries returned invalid JSON") from exc
        raise AssertionError("unreachable retry state")

    def _backoff(self, attempt: int) -> float:
        return min(self._retry_base_delay * (2**attempt), self._retry_max_delay)

    def _retry_after(self, response: httpx.Response, attempt: int) -> float:
        value = response.headers.get("Retry-After")
        if value:
            try:
                return min(max(0.0, float(value)), self._retry_max_delay)
            except ValueError:
                pass
        return self._backoff(attempt)
