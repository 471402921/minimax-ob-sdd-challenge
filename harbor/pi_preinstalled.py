"""Harbor 0.25.0 Pi adapter: use the pinned image installation, no per-trial nvm download.

All run/session/trajectory behavior is inherited from Harbor's Pi adapter.
Import via --agent pi_preinstalled:PreinstalledPi with PYTHONPATH=harbor/.
"""
from harbor.agents.installed.pi import Pi


class PreinstalledPi(Pi):
    async def install(self, environment):
        if self.mcp_servers:
            raise ValueError("MCP is intentionally unsupported in the isolated review pilot")
        if self._version != "1.4.0":
            raise ValueError("review image requires Pi version 1.4.0")
        result = await self.exec_as_agent(environment, command=(
            "set -euo pipefail; test \"$(pi --version)\" = 1.4.0; "
            "node --version; mkdir -p \"$HOME/.nvm\"; "
            "printf '%s\\n' '# Preinstalled Node is on PATH; no nvm download.' > \"$HOME/.nvm/nvm.sh\"; "
            "python3 -c \"import json,pathlib,subprocess; "
            "p=pathlib.Path('/input/runtime-profile.json'); "
            "enabled=p.is_file() and json.loads(p.read_text()).get('services') is True; "
            "subprocess.run(['python3','/opt/review/bootstrap.py','--ready-timeout','60'], "
            "check=True,timeout=90) if enabled else None\""
        ))
        if result is not None and getattr(result, 'return_code', 0) != 0:
            raise RuntimeError('Preinstalled runtime/service bootstrap failed; do not evaluate')
