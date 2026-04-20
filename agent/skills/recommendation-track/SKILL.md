---
name: recommendation-track
description: Query and explain the dual-track recommendation mechanism for ontology versions, including official recommendation and community highest-star recommendation. Use when the user asks which version is officially recommended, which version has the most stars, whether the tracks conflict, or which version should be adopted.
---

# Recommendation Track Skill

Use this skill for read-only questions about the two recommendation tracks:

- Official recommendation.
- Community highest-star recommendation.

## Preferred Entry Point

Use the gateway Agent API:

```http
POST /api/agent/query
```

Example:

```json
{
  "question": "student.json 当前官方推荐和社区推荐一致吗？",
  "project_id": "demo",
  "filename": "student.json"
}
```

## Direct Tools

If direct tool calls are needed, use `agent/git_query_tools.py`:

- `get_official_recommendation`
- `get_community_top_version`

For conflict analysis, call both tools and compare:

- `recommended_version_id`
- `stars`
- `community_rank`
- official metadata such as reason, operator, and time when present

## Parameter Rules

- Always pass `project_id`.
- Pass `filename` for JSON files.
- Pass `ontology_name` for business object names; allow Redis-backed ontology resolution to map it to a file.
- Do not infer a filename from an unrelated default if the user explicitly names another ontology.

## Answer Format

Use this structure:

```text
结论：官方推荐 Vx，社区推荐 Vy。二者一致/不一致。

官方轨道：...
社区轨道：...
建议：...
```

If both tracks point to the same version, state that it is a low-friction adoption candidate.
If they differ, explain the conflict and recommend human review rather than automatically changing recommendations.

## Safety

- This skill is read-only by default.
- Do not set or clear official recommendations unless the user explicitly asks for a governance action and the write endpoint exists.
- Do not mutate stars; star idempotency is a separate delivery item.
