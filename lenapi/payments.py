"""Optional sync and async payment-provider plugins for LenApi.

The adapters create hosted checkout sessions and verify transactions. They do
not store orders or mark them paid; that remains application business logic.
Install the transport with ``pip install 'lenapi[payments]'``.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from abc import ABC, abstractmethod
from decimal import Decimal, InvalidOperation
from typing import Any


def _minor_units(amount: int | float | Decimal | str, currency: str) -> int:
    """Convert a major-unit amount to a provider's integer minor units."""
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("amount must be a valid decimal number") from exc
    if not value.is_finite() or value <= 0:
        raise ValueError("amount must be a positive finite number")
    zero_decimal = {"BIF", "CLP", "DJF", "GNF", "JPY", "KMF", "KRW", "MGA", "PYG", "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF"}
    three_decimal = {"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"}
    exponent = 0 if currency.upper() in zero_decimal else 3 if currency.upper() in three_decimal else 2
    scaled = value * (10 ** exponent)
    if scaled != scaled.to_integral_value():
        raise ValueError(f"amount has more than {exponent} decimal places for {currency.upper()}")
    return int(scaled)


class PaymentPlugin(ABC):
    """Base class for async hosted-checkout provider plugins."""

    provider = "provider"

    def __init__(self, *, timeout: float = 30, client=None, sync_client=None,
                 base_url: str | None = None) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._provided_async_client = client
        self._async_client = client
        self._provided_sync_client = sync_client
        self._sync_client_instance = sync_client
        if client is None or sync_client is None:
            try:
                import httpx
            except ImportError as exc:
                raise ImportError("Payment plugins require `pip install 'lenapi[payments]'`") from exc
        self._timeout = timeout
        self.base_url = (base_url or self.default_base_url).rstrip("/")

    @property
    def _client(self):
        if self._async_client is None:
            import httpx
            self._async_client = httpx.AsyncClient(timeout=self._timeout)
        return self._async_client

    @property
    def _sync_client(self):
        if self._sync_client_instance is None:
            import httpx
            self._sync_client_instance = httpx.Client(timeout=self._timeout)
        return self._sync_client_instance

    @property
    @abstractmethod
    def default_base_url(self) -> str:
        raise NotImplementedError

    async def close(self) -> None:
        if self._provided_async_client is None and self._async_client is not None:
            await self._async_client.aclose()
            self._async_client = None
        if self._provided_sync_client is None and self._sync_client_instance is not None:
            self._sync_client_instance.close()
            self._sync_client_instance = None

    def close_sync(self) -> None:
        """Close the synchronous HTTP client, if it was created."""
        if self._provided_sync_client is None and self._sync_client_instance is not None:
            self._sync_client_instance.close()
            self._sync_client_instance = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close_sync()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        await self.close()

    @abstractmethod
    async def create_checkout(self, *, amount, currency: str, email: str,
                              reference: str, success_url: str | None = None,
                              cancel_url: str | None = None,
                              metadata: dict[str, Any] | None = None, **options) -> dict:
        """Create a hosted payment session. Amount is in major currency units."""

    @abstractmethod
    async def verify(self, reference: str) -> dict:
        """Ask the payment provider for the current transaction status."""


class Paystack(PaymentPlugin):
    """Paystack hosted checkout adapter."""

    provider = "paystack"
    default_base_url = "https://api.paystack.co"

    def __init__(self, secret_key: str | None = None, **kwargs) -> None:
        self.secret_key = secret_key or os.getenv("PAYSTACK_SECRET_KEY")
        if not self.secret_key:
            raise ValueError("Pass secret_key or set PAYSTACK_SECRET_KEY")
        super().__init__(**kwargs)

    @property
    def _headers(self):
        return {"Authorization": f"Bearer {self.secret_key}"}

    async def create_checkout(self, *, amount, currency="NGN", email: str,
                              reference: str, success_url=None, cancel_url=None,
                              metadata=None, **options) -> dict:
        payload = {"amount": str(_minor_units(amount, currency)), "currency": currency.upper(),
                   "email": email, "reference": reference, **options}
        if success_url:
            payload["callback_url"] = success_url
        if metadata is not None:
            payload["metadata"] = metadata
        response = await self._client.post(f"{self.base_url}/transaction/initialize",
                                           headers=self._headers, json=payload)
        response.raise_for_status()
        return response.json()

    def create_checkout_sync(self, *, amount, currency="NGN", email: str,
                             reference: str, success_url=None, cancel_url=None,
                             metadata=None, **options) -> dict:
        payload = {"amount": str(_minor_units(amount, currency)), "currency": currency.upper(),
                   "email": email, "reference": reference, **options}
        if success_url:
            payload["callback_url"] = success_url
        if metadata is not None:
            payload["metadata"] = metadata
        response = self._sync_client.post(f"{self.base_url}/transaction/initialize",
                                          headers=self._headers, json=payload)
        response.raise_for_status()
        return response.json()

    async def verify(self, reference: str) -> dict:
        response = await self._client.get(f"{self.base_url}/transaction/verify/{reference}",
                                          headers=self._headers)
        response.raise_for_status()
        return response.json()

    def verify_sync(self, reference: str) -> dict:
        response = self._sync_client.get(f"{self.base_url}/transaction/verify/{reference}",
                                         headers=self._headers)
        response.raise_for_status()
        return response.json()

    def verify_webhook(self, raw_body: bytes, signature: str) -> bool:
        expected = hmac.new(self.secret_key.encode(), raw_body, hashlib.sha512).hexdigest()
        return hmac.compare_digest(expected, signature)


class Flutterwave(PaymentPlugin):
    """Flutterwave Standard checkout adapter."""

    provider = "flutterwave"
    default_base_url = "https://api.flutterwave.com/v3"

    def __init__(self, secret_key: str | None = None, **kwargs) -> None:
        self.secret_key = secret_key or os.getenv("FLW_SECRET_KEY") or os.getenv("FLUTTERWAVE_SECRET_KEY")
        if not self.secret_key:
            raise ValueError("Pass secret_key or set FLW_SECRET_KEY")
        super().__init__(**kwargs)

    @property
    def _headers(self):
        return {"Authorization": f"Bearer {self.secret_key}"}

    async def create_checkout(self, *, amount, currency="NGN", email: str,
                              reference: str, success_url=None, cancel_url=None,
                              metadata=None, **options) -> dict:
        customer = {"email": email}
        payload = {"tx_ref": reference, "amount": str(amount), "currency": currency.upper(),
                   "customer": customer, **options}
        if success_url:
            payload["redirect_url"] = success_url
        if metadata is not None:
            payload["meta"] = metadata
        response = await self._client.post(f"{self.base_url}/payments",
                                           headers=self._headers, json=payload)
        response.raise_for_status()
        return response.json()

    def create_checkout_sync(self, *, amount, currency="NGN", email: str,
                             reference: str, success_url=None, cancel_url=None,
                             metadata=None, **options) -> dict:
        payload = {"tx_ref": reference, "amount": str(amount), "currency": currency.upper(),
                   "customer": {"email": email}, **options}
        if success_url:
            payload["redirect_url"] = success_url
        if metadata is not None:
            payload["meta"] = metadata
        response = self._sync_client.post(f"{self.base_url}/payments",
                                          headers=self._headers, json=payload)
        response.raise_for_status()
        return response.json()

    async def verify(self, transaction_id: str) -> dict:
        response = await self._client.get(f"{self.base_url}/transactions/{transaction_id}/verify",
                                          headers=self._headers)
        response.raise_for_status()
        return response.json()

    def verify_sync(self, transaction_id: str) -> dict:
        response = self._sync_client.get(f"{self.base_url}/transactions/{transaction_id}/verify",
                                         headers=self._headers)
        response.raise_for_status()
        return response.json()

    def verify_webhook(self, raw_body: bytes, signature: str) -> bool:
        secret_hash = os.getenv("FLW_WEBHOOK_SECRET_HASH", "")
        return bool(secret_hash) and hmac.compare_digest(secret_hash, signature)


class Stripe(PaymentPlugin):
    """Stripe Checkout Sessions adapter (hosted checkout)."""

    provider = "stripe"
    default_base_url = "https://api.stripe.com/v1"

    def __init__(self, secret_key: str | None = None, **kwargs) -> None:
        self.secret_key = secret_key or os.getenv("STRIPE_SECRET_KEY")
        if not self.secret_key:
            raise ValueError("Pass secret_key or set STRIPE_SECRET_KEY")
        super().__init__(**kwargs)

    @property
    def _auth(self):
        return (self.secret_key, "")

    async def create_checkout(self, *, amount, currency="usd", email: str,
                              reference: str, success_url=None, cancel_url=None,
                              metadata=None, **options) -> dict:
        if not success_url or not cancel_url:
            raise ValueError("Stripe Checkout requires success_url and cancel_url")
        currency = currency.lower()
        payload: dict[str, Any] = {
            "mode": "payment", "success_url": success_url, "cancel_url": cancel_url,
            "customer_email": email, "client_reference_id": reference,
            "line_items[0][quantity]": "1",
            "line_items[0][price_data][currency]": currency,
            "line_items[0][price_data][unit_amount]": str(_minor_units(amount, currency)),
            "line_items[0][price_data][product_data][name]": options.pop("product_name", "Payment"),
        }
        for key, value in (metadata or {}).items():
            payload[f"metadata[{key}]"] = str(value)
        payload.update(options)
        response = await self._client.post(f"{self.base_url}/checkout/sessions",
                                           auth=self._auth, data=payload)
        response.raise_for_status()
        return response.json()

    def create_checkout_sync(self, *, amount, currency="usd", email: str,
                             reference: str, success_url=None, cancel_url=None,
                             metadata=None, **options) -> dict:
        if not success_url or not cancel_url:
            raise ValueError("Stripe Checkout requires success_url and cancel_url")
        currency = currency.lower()
        payload: dict[str, Any] = {
            "mode": "payment", "success_url": success_url, "cancel_url": cancel_url,
            "customer_email": email, "client_reference_id": reference,
            "line_items[0][quantity]": "1",
            "line_items[0][price_data][currency]": currency,
            "line_items[0][price_data][unit_amount]": str(_minor_units(amount, currency)),
            "line_items[0][price_data][product_data][name]": options.pop("product_name", "Payment"),
        }
        for key, value in (metadata or {}).items():
            payload[f"metadata[{key}]"] = str(value)
        payload.update(options)
        response = self._sync_client.post(f"{self.base_url}/checkout/sessions",
                                          auth=self._auth, data=payload)
        response.raise_for_status()
        return response.json()

    async def verify(self, session_id: str) -> dict:
        response = await self._client.get(f"{self.base_url}/checkout/sessions/{session_id}",
                                          auth=self._auth)
        response.raise_for_status()
        return response.json()

    def verify_sync(self, session_id: str) -> dict:
        response = self._sync_client.get(f"{self.base_url}/checkout/sessions/{session_id}",
                                         auth=self._auth)
        response.raise_for_status()
        return response.json()

    def verify_webhook(self, raw_body: bytes, signature: str, *, tolerance_seconds: int = 300) -> bool:
        """Verify Stripe-Signature header value using Stripe's timestamped HMAC."""
        webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")
        if not webhook_secret:
            return False
        parts = [part.split("=", 1) for part in signature.split(",") if "=" in part]
        values: dict[str, list[str]] = {}
        for key, value in parts:
            values.setdefault(key, []).append(value)
        try:
            timestamp = int(values["t"][0])
        except (KeyError, ValueError, IndexError):
            return False
        if abs(time.time() - timestamp) > tolerance_seconds:
            return False
        signed = str(timestamp).encode() + b"." + raw_body
        expected = hmac.new(webhook_secret.encode(), signed, hashlib.sha256).hexdigest()
        return any(hmac.compare_digest(expected, candidate) for candidate in values.get("v1", []))


class Nomba(PaymentPlugin):
    """Nomba hosted checkout adapter using client-credentials access tokens."""

    provider = "nomba"
    default_base_url = "https://api.nomba.com"

    def __init__(self, client_id: str | None = None, client_secret: str | None = None,
                 account_id: str | None = None, **kwargs) -> None:
        self.client_id = client_id or os.getenv("NOMBA_CLIENT_ID")
        self.client_secret = client_secret or os.getenv("NOMBA_CLIENT_SECRET")
        self.account_id = account_id or os.getenv("NOMBA_ACCOUNT_ID")
        if not all((self.client_id, self.client_secret, self.account_id)):
            raise ValueError("Pass client_id, client_secret, account_id or set NOMBA_CLIENT_ID, NOMBA_CLIENT_SECRET and NOMBA_ACCOUNT_ID")
        self._token: str | None = None
        self._token_expires_at = 0.0
        super().__init__(**kwargs)

    async def _access_token(self) -> str:
        if self._token and time.monotonic() < self._token_expires_at - 30:
            return self._token
        response = await self._client.post(
            f"{self.base_url}/v1/auth/token/issue", headers={"accountId": self.account_id},
            json={"grant_type": "client_credentials", "client_id": self.client_id,
                  "client_secret": self.client_secret},
        )
        response.raise_for_status()
        result = response.json()
        data = result.get("data", result)
        self._token = data.get("access_token") or data.get("token")
        if not self._token:
            raise ValueError("Nomba token response did not include an access token")
        self._token_expires_at = time.monotonic() + float(data.get("expires_in", 3600))
        return self._token

    def _access_token_sync(self) -> str:
        if self._token and time.monotonic() < self._token_expires_at - 30:
            return self._token
        response = self._sync_client.post(
            f"{self.base_url}/v1/auth/token/issue", headers={"accountId": self.account_id},
            json={"grant_type": "client_credentials", "client_id": self.client_id,
                  "client_secret": self.client_secret},
        )
        response.raise_for_status()
        result = response.json()
        data = result.get("data", result)
        self._token = data.get("access_token") or data.get("token")
        if not self._token:
            raise ValueError("Nomba token response did not include an access token")
        self._token_expires_at = time.monotonic() + float(data.get("expires_in", 3600))
        return self._token

    async def create_checkout(self, *, amount, currency="NGN", email: str,
                              reference: str, success_url=None, cancel_url=None,
                              metadata=None, **options) -> dict:
        token = await self._access_token()
        order = {"customerEmail": email, "amount": str(amount), "currency": currency.upper(),
                 "orderReference": reference, "accountId": self.account_id, **options}
        if success_url:
            order["callbackUrl"] = success_url
        if metadata is not None:
            order["orderMetaData"] = metadata
        response = await self._client.post(
            f"{self.base_url}/v1/checkout/order",
            headers={"Authorization": f"Bearer {token}", "accountId": self.account_id},
            json={"order": order},
        )
        response.raise_for_status()
        return response.json()

    def create_checkout_sync(self, *, amount, currency="NGN", email: str,
                             reference: str, success_url=None, cancel_url=None,
                             metadata=None, **options) -> dict:
        token = self._access_token_sync()
        order = {"customerEmail": email, "amount": str(amount), "currency": currency.upper(),
                 "orderReference": reference, "accountId": self.account_id, **options}
        if success_url:
            order["callbackUrl"] = success_url
        if metadata is not None:
            order["orderMetaData"] = metadata
        response = self._sync_client.post(
            f"{self.base_url}/v1/checkout/order",
            headers={"Authorization": f"Bearer {token}", "accountId": self.account_id},
            json={"order": order},
        )
        response.raise_for_status()
        return response.json()

    async def verify(self, reference: str) -> dict:
        token = await self._access_token()
        response = await self._client.get(
            f"{self.base_url}/v1/checkout/transaction",
            headers={"Authorization": f"Bearer {token}", "accountId": self.account_id},
            params={"idType": "ORDER_REFERENCE", "id": reference},
        )
        response.raise_for_status()
        return response.json()

    def verify_sync(self, reference: str) -> dict:
        token = self._access_token_sync()
        response = self._sync_client.get(
            f"{self.base_url}/v1/checkout/transaction",
            headers={"Authorization": f"Bearer {token}", "accountId": self.account_id},
            params={"idType": "ORDER_REFERENCE", "id": reference},
        )
        response.raise_for_status()
        return response.json()


_PROVIDERS = {"nomba": Nomba, "flutterwave": Flutterwave, "stripe": Stripe, "paystack": Paystack}


def payment_provider(name: str, **config) -> PaymentPlugin:
    """Load a registered provider plugin by name.

    Example: ``gateway = payment_provider("paystack")``. Applications can
    register custom adapters by extending ``PaymentPlugin`` and calling
    ``register_payment_provider``.
    """
    try:
        plugin = _PROVIDERS[name.lower()]
    except KeyError as exc:
        raise ValueError(f"Unknown payment provider {name!r}; choose from {', '.join(sorted(_PROVIDERS))}") from exc
    return plugin(**config)


class Payments:
    """Developer-facing facade over LenApi's payment provider plugins.

    Choose a built-in provider by name, then use the same checkout and
    verification methods for every provider::

        with Payments("paystack") as payments:
            checkout = payments.create_checkout(...)
            status = payments.verify("order-123")

        async with Payments("paystack") as payments:
            checkout = await payments.acreate_checkout(...)
            status = await payments.averify("order-123")

    Provider-specific credentials can be passed as keyword arguments or read
    from environment variables. No provider class is needed in application code.
    """

    def __init__(self, provider: str, **config) -> None:
        self.provider = provider.strip().lower()
        self._plugin = payment_provider(self.provider, **config)

    def create_checkout(self, *, amount, currency: str, email: str,
                        reference: str, success_url: str | None = None,
                        cancel_url: str | None = None,
                        metadata: dict[str, Any] | None = None, **options) -> dict:
        """Synchronously create a hosted checkout session."""
        return self._plugin.create_checkout_sync(
            amount=amount, currency=currency, email=email, reference=reference,
            success_url=success_url, cancel_url=cancel_url, metadata=metadata,
            **options,
        )

    async def acreate_checkout(self, *, amount, currency: str, email: str,
                               reference: str, success_url: str | None = None,
                               cancel_url: str | None = None,
                               metadata: dict[str, Any] | None = None, **options) -> dict:
        """Asynchronously create a hosted checkout session."""
        return await self._plugin.create_checkout(
            amount=amount, currency=currency, email=email, reference=reference,
            success_url=success_url, cancel_url=cancel_url, metadata=metadata,
            **options,
        )

    def verify(self, reference: str) -> dict:
        """Synchronously retrieve transaction status from the provider."""
        return self._plugin.verify_sync(reference)

    async def averify(self, reference: str) -> dict:
        """Asynchronously retrieve transaction status from the provider."""
        return await self._plugin.verify(reference)

    def verify_webhook(self, raw_body: bytes, signature: str, **options) -> bool:
        """Check a provider webhook signature when that adapter supports it."""
        verifier = getattr(self._plugin, "verify_webhook", None)
        if verifier is None:
            raise NotImplementedError(f"{self.provider} webhook signature verification is not implemented")
        return verifier(raw_body, signature, **options)

    async def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        await self._plugin.close()

    def close_sync(self) -> None:
        """Close the synchronous HTTP connection pool."""
        self._plugin.close_sync()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close_sync()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        await self.close()


def register_payment_provider(name: str, plugin: type[PaymentPlugin]) -> None:
    """Register a custom provider adapter for ``payment_provider``."""
    key = name.strip().lower()
    if not key or not issubclass(plugin, PaymentPlugin):
        raise ValueError("name must be non-empty and plugin must subclass PaymentPlugin")
    _PROVIDERS[key] = plugin


__all__ = ["Payments", "PaymentPlugin", "Nomba", "Flutterwave", "Stripe", "Paystack",
           "payment_provider", "register_payment_provider"]
