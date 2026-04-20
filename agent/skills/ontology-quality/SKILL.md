---
name: ontology-quality
description: Evaluate ontology data quality with emphasis on probability fields and probability inference readiness. Use when the user asks whether an ontology has probability, whether probability inference should be rerun, which ontologies are missing probability, or how probability writeback should be validated.
---

# Ontology Quality Skill

Use this skill for probability and ontology content quality checks.

## Current Capability Boundary

The current Agent toolset can detect probability-related governance gaps through:

- `find_governance_gaps`
- `get_version_content`
- `get_file_timeline`

Probability inference and writeback are primarily handled by XiaoGuGit's write path:

```http
POST /xg/write-and-infer
```

Do not claim that the Agent has a dedicated probability inference tool unless one is implemented.

## Preferred Read-Only Checks

Use the gateway Agent API:

```http
POST /api/agent/query
```

Examples:

```json
{
  "question": "哪些本体缺少 probability 字段？",
  "project_id": "demo"
}
```

```json
{
  "question": "学生本体当前版本的 probability 是多少？",
  "project_id": "demo",
  "ontology_name": "学生"
}
```

## Direct Tool Strategy

- Use `get_version_content` when the user asks for one ontology's current probability.
- Use `find_governance_gaps` when the user asks for missing or low probability across a project.
- Use `get_file_timeline` when verifying whether a recent write produced a probability writeback.

## Quality Checks

When inspecting ontology JSON, check:

- `name` exists and is meaningful.
- `agent` exists.
- `abilities` is a non-empty array.
- `interactions` is an array with valid `target` and `type`.
- `probability` exists when the ontology has already gone through inference.
- `probability` is a percentage string such as `98%`.
- `reason` should not be written into ontology JSON when only probability is required.

## Answer Format

Use:

```text
结论：...

证据：...

建议：...
```

If probability is missing, explain whether this likely means:

- the ontology was created before probability writeback existed,
- inference failed,
- writeback failed,
- or the current version was modified without rerunning `write-and-infer`.

## Safety

- Do not call LLM probability inference unless the user explicitly asks for mutation or inference.
- Do not return raw LLM/provider errors to the user; use a safe retry suggestion.
- If writeback is required, ensure it updates the current version rather than creating a new version, matching the existing project behavior.
