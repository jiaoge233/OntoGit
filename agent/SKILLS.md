# Agent Skills

This directory contains repo-local skills for the data-infra Agent. They document how an AI agent should use the existing gateway Agent API and Python tools without rediscovering the workflow each time.

## Skills

- `agent/skills/ontology-version`: version timeline, version content, and version diff queries.
- `agent/skills/recommendation-track`: official recommendation and community highest-star recommendation queries.
- `agent/skills/ontology-governance`: governance gap diagnosis for a project or ontology.
- `agent/skills/ontology-quality`: probability and ontology JSON quality checks.

## Runtime Entry Point

Use gateway first:

```http
POST /api/agent/query
```

Service calls should include:

```http
X-API-Key: <GATEWAY_SERVICE_API_KEY>
```

For direct local execution, use:

```bash
python agent/run_git_query_agent.py "student.json 当前社区星标最高的版本是谁？" --project-id demo --base-url http://127.0.0.1:8080 --api-key local-gateway-key
```

## Implementation Notes

- The skills are read-only by default.
- Mutating actions such as rollback, write, star, and official recommendation changes should remain explicit user actions.
- The Agent is packaged into the gateway container; runtime secrets should be configured in `gateway/.env`, not in tracked skill files.
