---
name: ontology-version
description: Query ontology Git version history, current or historical version content, and JSON field-level diffs through the data-infra gateway Agent/tool chain. Use when the user asks about ontology evolution, recent changes, a specific version's content, or comparing two versions in XiaoGuGit.
---

# Ontology Version Skill

Use this skill to answer read-only questions about ontology version history in the data-infra Git repository.

## Preferred Entry Point

Prefer the gateway Agent API when available:

```http
POST /api/agent/query
```

Required authentication for service calls:

```http
X-API-Key: <GATEWAY_SERVICE_API_KEY>
```

Use a natural-language question and include the most specific known context:

```json
{
  "question": "学校本体最近发生了哪些变化？",
  "project_id": "demo",
  "ontology_name": "学校"
}
```

## Direct Tools

If bypassing the planner, use `agent/git_query_tools.py`:

- `get_file_timeline`: version timeline, latest version, recent changes.
- `get_version_content`: current working copy or a specified historical version.
- `compare_versions`: JSON field-level diff between two versions.

## Parameter Rules

- Always pass `project_id`.
- Prefer `filename` when the user names a JSON file, for example `student.json`.
- Use `ontology_name` when the user names a business object, for example `学生`, `school`, or `学校`.
- Do not assume Chinese and English are different ontologies; rely on ontology resolution.
- For `compare_versions`, require both `left_version_id` and `right_version_id`.
- For `get_version_content`, omit `version_id` only when the user asks for current content.

## Answer Format

Lead with the conclusion, then provide evidence:

- Ontology/file.
- Version id or compared version ids.
- Commit message.
- Committer and time when available.
- Important JSON changes, not full raw payload unless requested.

## Safety

- This skill is read-only.
- Do not call rollback, write, star, recommendation-setting, or probability inference endpoints from this skill.
- If the requested ontology cannot be resolved, ask for either `filename` or ontology name rather than guessing.
