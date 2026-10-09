"""Output throughput: duration-weighted averages and missing samples."""

import asyncio

import pytest
from textual.app import App

from ddtui.providers import CompletionTokenDetails, ProviderUsage
from ddtui.state import TokenCounter
from ddtui.widgets import StatusBar


def test_average_weights_duration_and_does_not_double_count_reasoning():
    counter = TokenCounter()
    counter.add(ProviderUsage(10_000, 100, CompletionTokenDetails(60)), elapsed=2, ttft=1)
    counter.add(ProviderUsage(40_000, 100, CompletionTokenDetails(70)), elapsed=8, ttft=3)

    # Output / total request time, rather than averaging the two rates
    # (50 and 12.5), counting input tokens, or adding reasoning twice.
    assert counter.average_tokens_per_second == 20
    # Decode clock excludes prefill: 200 tokens over (2-1) + (8-3) seconds.
    assert counter.decode_tokens_per_second == pytest.approx(200 / 6)
    assert counter.last_ttft == 3
    assert counter.last_reasoning == 70
    assert counter.last_prompt == 40_000


def test_missing_usage_or_duration_does_not_bias_average():
    counter = TokenCounter()
    counter.add(None, elapsed=100)
    counter.add(ProviderUsage(completion_tokens=1000))
    counter.add(ProviderUsage(completion_tokens=1000), elapsed=0)
    counter.add(ProviderUsage(completion_tokens=1000), elapsed=-1)
    assert counter.average_tokens_per_second is None

    counter.add(ProviderUsage(completion_tokens=100), elapsed=2)
    counter.add(None, elapsed=100)
    counter.add(ProviderUsage(completion_tokens=1000))
    assert counter.average_tokens_per_second == 50

    counter.add(ProviderUsage(completion_tokens=0), elapsed=2)
    assert counter.average_tokens_per_second == 25


def test_missing_or_invalid_ttft_keeps_decode_rate_undiluted():
    counter = TokenCounter()
    # Without a first-token timestamp the decode aggregate must not
    # absorb the (possibly prefill-heavy) wall time.
    counter.add(ProviderUsage(completion_tokens=100), elapsed=2)
    assert counter.decode_tokens_per_second is None
    assert counter.last_ttft is None

    counter.add(ProviderUsage(completion_tokens=100), elapsed=2, ttft=1)
    assert counter.decode_tokens_per_second == 100
    assert counter.last_ttft == 1

    # ttft >= elapsed is nonsense (stream ended before first token?):
    # skip the sample and surface "no reading" for the latest request.
    counter.add(ProviderUsage(completion_tokens=100), elapsed=2, ttft=5)
    assert counter.decode_tokens_per_second == 100
    assert counter.last_ttft is None


@pytest.mark.parametrize("busy", [False, True])
def test_footer_rate_placeholder_and_controls(tmp_path, busy):
    class Harness(App):
        def compose(self):
            yield StatusBar(str(tmp_path))

    async def run():
        app = Harness()
        async with app.run_test(size=(220, 5)) as pilot:
            bar = app.query_one(StatusBar)
            counter = TokenCounter()
            bar.render_status(counter, busy=busy)
            assert "生成 -- tok/s" in str(bar.render())

            counter.add(ProviderUsage(1000, 84), elapsed=2, ttft=1)
            bar.render_status(counter, busy=busy, queued=2, steer=1, explore=True)
            await pilot.pause()
            text = str(bar.render())
            assert "生成 84.0 tok/s" in bar.render_line(0).text
            assert "首响 1.0s" in bar.render_line(0).text
            assert "Context 1,084" in text
            assert "排队 2" in text and "steer 1" in text and "explore: on" in text

    asyncio.run(run())
