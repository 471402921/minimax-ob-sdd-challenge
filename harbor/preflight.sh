#!/bin/bash
# Free Docker network/image debris before a large batch. Never touches running containers.
set -euo pipefail
echo "networks before: $(sudo -n docker network ls --filter type=custom -q | wc -l)"
sudo -n docker network prune -f >/dev/null
echo "networks after:  $(sudo -n docker network ls --filter type=custom -q | wc -l)"
echo "dangling images: $(sudo -n docker images -f dangling=true -q | wc -l)"
sudo -n docker image prune -f >/dev/null 2>&1 || true
free -g | awk 'NR==2{print "available_gib",$7}'
df -h / | tail -1
