#!/usr/bin/env bash
set -euo pipefail

# Thin local copy of the Composite Refactorings 2020 Neo4j start script.
# The extracted Neo4j 3.1 runtime lives outside this repo.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REVAMP_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
ROOT_DIR="$(cd "$REVAMP_ROOT/../datasets/composite_refactorings_2020_runtime" && pwd)"
EXTRACTED_DIR="$ROOT_DIR/work/neo4j-community-3.1.0-msr-2019"
CONF_FILE="$EXTRACTED_DIR/conf/neo4j.conf"

if [[ -f "$HOME/.sdkman/bin/sdkman-init.sh" ]]; then
  set +u
  # shellcheck disable=SC1091
  source "$HOME/.sdkman/bin/sdkman-init.sh"
  sdk env "$ROOT_DIR" >/dev/null || true
  set -u
fi

# Force the exact Java pinned in .sdkmanrc, because the legacy Neo4j launcher
# may otherwise resolve a newer system JDK on macOS.
JAVA_CANDIDATE="$(awk -F= '/^java=/{print $2}' "$ROOT_DIR/.sdkmanrc")"
if [[ -n "${JAVA_CANDIDATE:-}" ]]; then
  export JAVA_HOME="$HOME/.sdkman/candidates/java/$JAVA_CANDIDATE"
  export PATH="$JAVA_HOME/bin:$PATH"
  export JAVA_CMD="$JAVA_HOME/bin/java"
fi

if [[ ! -d "$EXTRACTED_DIR" ]]; then
  echo "Neo4j runtime not extracted yet. Run extract_neo4j.sh in:" >&2
  echo "  $ROOT_DIR" >&2
  exit 1
fi

cp "$CONF_FILE" "$CONF_FILE.bak" 2>/dev/null || true

python3 - <<'PY' "$CONF_FILE"
from pathlib import Path
import sys
path = Path(sys.argv[1])
text = path.read_text()
replacements = {
    '#dbms.connector.http.listen_address=:7474': 'dbms.connector.http.listen_address=:7474',
    '#dbms.connector.bolt.listen_address=:7687': 'dbms.connector.bolt.listen_address=:7687',
    'dbms.connector.https.enabled=true': 'dbms.connector.https.enabled=false',
    '#dbms.memory.heap.initial_size=512m': 'dbms.memory.heap.initial_size=512m',
    '#dbms.memory.heap.max_size=512m': 'dbms.memory.heap.max_size=512m',
}
for old, new in replacements.items():
    text = text.replace(old, new)
path.write_text(text)
PY

cd "$EXTRACTED_DIR"
./bin/neo4j start

echo "Neo4j start requested"
echo "HTTP: http://127.0.0.1:7474"
echo "Bolt: bolt://127.0.0.1:7687"
echo "Use dataset/scripts/stop_neo4j.sh to stop it"
