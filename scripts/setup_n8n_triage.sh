#!/usr/bin/env bash
# n8n AI Ticket Triage Setup — TimeTrax Integration
# Run when Docker is available (requires Docker Desktop or Docker Engine)
#
# This sets up:
#   n8n (self-hosted AI workflow automation) + TimeTrax webhook
#
# Prerequisites:
#   - Docker installed and running
#   - TimeTrax already running on localhost:8001
#   - TIMETRAX_API_KEY environment variable or set below

set -e

echo "=== n8n + TimeTrax AI Ticket Triage Setup ==="
echo ""

# ── Check prerequisites ──
if ! command -v docker &>/dev/null; then
    echo "❌ Docker not found. Install Docker Desktop first:"
    echo "   https://www.docker.com/products/docker-desktop/"
    echo ""
    echo "When Docker is ready, re-run this script."
    exit 1
fi

if ! docker info &>/dev/null; then
    echo "❌ Docker daemon not running. Start Docker Desktop first."
    exit 1
fi

echo "✅ Docker available"

# ── Check TimeTrax ──
if curl -sf http://localhost:8001/api/tickets/ > /dev/null 2>&1; then
    echo "✅ TimeTrax API reachable on port 8001"
else
    echo "⚠️  TimeTrax not detected on port 8001. Verify it's running."
    echo "   Continue anyway (n8n can be configured later)."
fi

# ── Config ──
# Set TIMETRAX_API_KEY in your environment before running (see .env.example).
if [ -z "$TIMETRAX_API_KEY" ]; then
    echo "❌ TIMETRAX_API_KEY not set. Export it before running this script."
    exit 1
fi
N8N_DIR="$(dirname "$0")/n8n"
N8N_PORT=5678

mkdir -p "$N8N_DIR"

# ── Write docker-compose ──
cat > "$N8N_DIR/docker-compose.yml" << 'YAMLEOF'
version: "3.8"
services:
  n8n:
    image: n8nio/n8n:latest
    container_name: timetrax-n8n
    restart: unless-stopped
    ports:
      - "5678:5678"
    environment:
      - N8N_HOST=localhost
      - N8N_PORT=5678
      - N8N_PROTOCOL=http
      - WEBHOOK_URL=http://localhost:5678/
      - N8N_METRICS=false
      - EXECUTIONS_DATA_PRUNE=true
      - EXECUTIONS_DATA_MAX_AGE=168
    volumes:
      - ./data:/home/node/.n8n
YAMLEOF

echo "✅ docker-compose.yml written to $N8N_DIR"

# ── Write TimeTrax AI Agent workflow ──
mkdir -p "$N8N_DIR/workflows"

cat > "$N8N_DIR/workflows/timetrax-triage.json" << 'WFEOF'
{
  "name": "TimeTrax AI Ticket Triage",
  "nodes": [
    {
      "name": "Webhook - Ticket Created",
      "type": "n8n-nodes-base.webhook",
      "typeVersion": 1,
      "position": [250, 300],
      "parameters": {
        "path": "timetrax-triage",
        "options": {}
      }
    },
    {
      "name": "AI Triage Classifier",
      "type": "n8n-nodes-base.aiAgent",
      "typeVersion": 1,
      "position": [450, 300],
      "parameters": {
        "prompt": "You are a helpdesk triage agent. Classify this ticket and respond with JSON only:\n\nTitle: {{$json.title}}\nDescription: {{$json.description}}\n\nClassify as one of: password_reset, hardware_issue, software_bug, account_access, security_alert, general_inquiry\n\nRespond with JSON: {\"category\": \"...\", \"priority\": \"H|M|L\", \"summary\": \"1-line summary\", \"response_suggestion\": \"what to do first\"}",
        "model": "gpt-4o-mini",
        "apiKey": "={{ $env.OPENAI_API_KEY }}"
      }
    },
    {
      "name": "Auto-Reply (Password Reset)",
      "type": "n8n-nodes-base.httpRequest",
      "typeVersion": 1,
      "position": [650, 200],
      "parameters": {
        "method": "POST",
        "url": "http://host.docker.internal:8001/api/tickets/{{ $json.ticket_id }}/comments/",
        "sendHeaders": true,
        "headerParameters": {
          "parameters": [
            {"name": "Authorization", "value": "Bearer TIMETRAX_API_KEY"},
            {"name": "Content-Type", "value": "application/json"}
          ]
        },
        "sendBody": true,
        "bodyParameters": {
          "parameters": [
            {"name": "body", "value": "{{ $json.auto_response }}"},
            {"name": "source", "value": "system"}
          ]
        }
      }
    }
  ],
  "connections": {
    "Webhook - Ticket Created": {
      "main": [[{"node": "AI Triage Classifier", "type": "main", "index": 0}]]
    },
    "AI Triage Classifier": {
      "main": [[{"node": "Auto-Reply (Password Reset)", "type": "main", "index": 0}]]
    }
  }
}
WFEOF

echo "✅ TimeTrax triage workflow written"

# ── Launch ──
echo ""
echo "=== Ready to launch ==="
echo "  cd $N8N_DIR && docker compose up -d"
echo "  → n8n dashboard: http://localhost:$N8N_PORT"
echo ""
echo "After launch, configure:"
echo "  1. Set OpenAI API key in n8n -> Settings -> environment variables"
echo "  2. Activate the 'TimeTrax AI Ticket Triage' workflow"
echo "  3. Set TimeTrax to POST webhooks to http://host.docker.internal:$N8N_PORT/webhook/timetrax-triage"
echo ""
echo "The AI agent will:"
echo "  - Classify new tickets by category + priority"
echo "  - Auto-suggest responses for common requests (password resets, etc.)"
echo "  - Post triage results as comments back to TimeTrax"
