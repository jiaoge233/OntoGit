---
name: ontology-governance
description: Inspect ontology governance gaps across a project or a single ontology, including missing official recommendation, official/community recommendation divergence, missing or low probability, stale versions, deleted current state, and complex version trees. Use when the user asks what remains to govern, what is risky, or what should be fixed before delivery.
---

# Ontology Governance Skill

Use this skill to produce actionable governance findings for an ontology Git project.

## Preferred Entry Point

Use the gateway Agent API:

```http
POST /api/agent/query
```

Examples:

```json
{
  "question": "demo 项目有哪些治理缺口？",
  "project_id": "demo"
}
```

```json
{
  "question": "检查学校本体还有哪些治理缺口。",
  "project_id": "demo",
  "ontology_name": "学校"
}
```

## Direct Tool

Use `agent/git_query_tools.py`:

- `find_governance_gaps`

This tool scans one ontology or the whole project, depending on whether `filename`/`ontology_name` is provided.

## Findings To Surface

Prioritize findings in this order:

1. Missing official recommendation.
2. Official recommendation conflicts with community highest-star version.
3. Missing `probability` field.
4. Low probability value.
5. Deleted current state.
6. Complex or branched version tree that may need review.
7. Stale or long-unreviewed ontology when time data is available.

## Answer Format

Use short, actionable output:

```text
结论：发现 N 个治理缺口，优先处理 A、B。

高优先级：
- ...

中优先级：
- ...

建议下一步：
- ...
```

Include evidence for each finding:

- `project_id`
- `filename` or ontology name
- related version ids
- official/community status
- probability value when available

## Safety

- Treat this skill as read-only diagnosis.
- Do not write recommendations, rollback, star, or modify ontology data from this skill.
- If a fix is requested, name the required endpoint/tool explicitly before executing it.
