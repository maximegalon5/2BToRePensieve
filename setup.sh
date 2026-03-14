#!/usr/bin/env bash
# Local development setup for Isaac / Open Brain
# Requires: python3.12-venv, supabase CLI

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "==> Checking dependencies..."
if ! command -v python3 &>/dev/null; then
  echo "ERROR: python3 not found" >&2; exit 1
fi
if ! command -v supabase &>/dev/null; then
  echo "ERROR: supabase CLI not found. Install: https://supabase.com/docs/guides/cli" >&2; exit 1
fi

echo "==> Creating Python virtual environment..."
python3 -m venv venv

echo "==> Installing Python dependencies..."
# shellcheck disable=SC1091
source venv/bin/activate
pip install --quiet --upgrade pip
pip install -r requirements.txt

echo ""
echo "==> Verifying Supabase connectivity..."
python3 - <<'EOF'
from dotenv import load_dotenv; load_dotenv()
from open_brain.config import load_open_brain_config
from open_brain.db import get_client
cfg = load_open_brain_config()
if not cfg.supabase.url or not cfg.supabase.service_role_key:
    print("WARNING: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set in .env")
else:
    client = get_client(cfg)
    result = client.rpc('get_top_connected_entities', {'result_limit': 3}).execute()
    print(f"  Connected! Top entities in graph: {len(result.data)}")
EOF

echo ""
echo "==> Done. Activate the environment with:"
echo "      source venv/bin/activate"
echo ""
echo "Optional: link Supabase CLI to the project:"
echo "  supabase login --token sbp_<your-token>"
echo "  supabase link --project-ref yagwhyhhdsxctjjnasox"
