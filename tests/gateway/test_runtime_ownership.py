"""Real process exclusion before writable gateway construction."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.linux_only
def test_losing_start_never_constructs_writable_runner(tmp_path):
    from gateway.status import acquire_gateway_runtime_lock, release_gateway_runtime_lock
    assert acquire_gateway_runtime_lock()
    code = '''
import asyncio
from pathlib import Path
import os
import gateway.run as run
from gateway.config import GatewayConfig
class Witness:
    def __init__(self, config):
        Path(os.environ['WITNESS']).write_text('writable init reached')
        raise RuntimeError('initializer reached')
run.GatewayRunner = Witness
try:
    result = asyncio.run(run.start_gateway(GatewayConfig(), verbosity=None))
except RuntimeError:
    result = True
print('CLAIMED', result)
'''
    witness = tmp_path / 'witness'
    try:
        result = subprocess.run([sys.executable, '-c', code], env={**os.environ, 'WITNESS': str(witness)},
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert not witness.exists(), result.stdout + result.stderr
        assert 'CLAIMED False' in result.stdout
    finally:
        release_gateway_runtime_lock()


@pytest.mark.linux_only
def test_profile_reservations_unwind_without_releasing_another_owner(tmp_path):
    from gateway import runtime_ownership
    homes = [tmp_path / 'a', tmp_path / 'b']
    for home in homes:
        home.mkdir()
    blocker = runtime_ownership.ProfileOwnership()
    blocker.reserve([homes[1]])
    contender = runtime_ownership.ProfileOwnership()
    with pytest.raises(runtime_ownership.OwnershipConflict):
        contender.reserve(reversed(homes))
    free = runtime_ownership.ProfileOwnership()
    free.reserve([homes[0]])
    contender.close()
    with pytest.raises(runtime_ownership.OwnershipConflict):
        contender.reserve([homes[1]])
    blocker.close()
    contender.reserve([homes[1]])
    blocker.close()  # late cleanup must not affect replacement
    with pytest.raises(runtime_ownership.OwnershipConflict):
        blocker.reserve([homes[1]])
    free.close()
    contender.close()
