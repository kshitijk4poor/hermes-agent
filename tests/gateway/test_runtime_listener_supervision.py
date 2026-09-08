"""A dead session listener must never leave a ready gateway behind."""
import asyncio

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize('listener_failure', [False, True])
async def test_runtime_wait_observes_listener_exit(tmp_path, listener_failure):
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway import run_runtime
    from gateway.runtime_ownership import process_ownership
    from hermes_constants import get_hermes_home

    process_ownership.reserve([get_hermes_home()])
    runner = GatewayRunner(GatewayConfig())
    waiter = None
    try:
        await run_runtime.initialize_gateway_runtime(runner)
        await run_runtime.start_gateway_runtime_api(runner)
        assert await runner.start()
        run_runtime.publish_gateway_runtime_ready(runner)
        waiter = asyncio.create_task(run_runtime.wait_gateway_runtime(runner))
        if listener_failure:
            # Stop the actual listener independently of the runner lifecycle.
            runner.session_api.server.should_exit = True
            with pytest.raises(RuntimeError, match='session API stopped unexpectedly'):
                await asyncio.wait_for(waiter, 10)
            assert runner.session_runtime_descriptor['state'] != 'ready'
            assert not runner.session_runtime_descriptor['capabilities']
        else:
            await runner.stop()
            await asyncio.wait_for(waiter, 10)
    finally:
        if waiter is not None and not waiter.done():
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        await runner.stop()
        process_ownership.close()
