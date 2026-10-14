from unittest.mock import AsyncMock, patch

from posthog.llm.gateway_usage import GatewayRequestCost, fetch_gateway_cost


async def test_internal_gateway_usage_ignores_environment_proxies() -> None:
    with patch("aiohttp.ClientSession._request", autospec=True) as request:
        request.return_value.__aenter__.return_value = request.return_value
        request.return_value.status = 200
        request.return_value.json = AsyncMock(
            return_value={"request_id": "request-1", "cost_usd": "0.001", "model": "model", "provider": "provider"}
        )
        cost = await fetch_gateway_cost("request-1", base_url="http://gateway:8080/v1", api_key="test-key")

    assert cost == GatewayRequestCost(model="model", provider="provider", cost_microusd=1_000)
    assert request.call_args.args[0].trust_env is False
