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
        await self.exec_as_agent(environment, command=(
            "set -euo pipefail; test \"$(pi --version)\" = 1.4.0; "
            "node --version; mkdir -p \"$HOME/.nvm\"; "
            "printf '%s\\n' '# Preinstalled Node is on PATH; no nvm download.' > \"$HOME/.nvm/nvm.sh\""
        ))
