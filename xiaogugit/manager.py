import json
import logging
import os
import re
import subprocess
from datetime import datetime

import git

try:
    import redis
except Exception:  # pragma: no cover - optional dependency fallback
    redis = None


logger = logging.getLogger("xiaogugit.manager")


class XiaoGuGitManager:
    META_FILE = "project_meta.json"
    STARS_DIRNAME = ".xg_meta"
    STARS_FILE_SUFFIX = "_version_stars.json"
    INTERNAL_FILE_PREFIXES = ("_inference/",)
    DEFAULT_STATUS = "开发中"
    ALLOWED_STATUS = {"开发中", "测试中", "已完成", "已暂停", "已归档", "已回滚"}
    VERSION_META_FILENAME = "XG-Filename"
    VERSION_META_ID = "XG-VersionId"
    VERSION_META_BASE = "XG-BaseVersion"
    VERSION_META_OBJECT = "XG-ObjectName"
    VERSION_META_COMMITTER = "XG-CommitterName"

    def __init__(
        self,
        root_dir=None,
        redis_enabled=False,
        redis_host="127.0.0.1",
        redis_port=6379,
        redis_password="",
        redis_db=0,
        redis_key_prefix="xg",
        redis_socket_timeout=1.5,
    ):
        self.root_dir = os.path.abspath(root_dir or "./storage")
        os.makedirs(self.root_dir, exist_ok=True)
        self._stars_dir = os.path.join(self.root_dir, self.STARS_DIRNAME)
        os.makedirs(self._stars_dir, exist_ok=True)
        self._redis_key_prefix = (redis_key_prefix or "xg").strip() or "xg"
        self._redis = None
        if redis_enabled and redis is not None:
            try:
                self._redis = redis.Redis(
                    host=redis_host,
                    port=int(redis_port),
                    password=redis_password or None,
                    db=int(redis_db),
                    decode_responses=True,
                    socket_timeout=float(redis_socket_timeout),
                    socket_connect_timeout=float(redis_socket_timeout),
                )
                self._redis.ping()
                logger.info(
                    "Redis cache enabled for XiaoGuGit at %s:%s/%s",
                    redis_host,
                    redis_port,
                    redis_db,
                )
            except Exception as exc:
                logger.warning("Redis unavailable, falling back to file/Git only: %s", exc)
                self._redis = None

    def _redis_call(self, callback, default=None):
        if self._redis is None:
            return default
        try:
            return callback(self._redis)
        except Exception as exc:
            logger.warning("Redis operation failed, falling back to current implementation: %s", exc)
            return default

    # Redis key design for official recommendation:
    # - key: {prefix}:official:{project_id}:{filename}
    # - type: HASH
    # - fields: version_id, reason, operator, updated_at
    # - ttl: no TTL. Official recommendation is governance state and should persist
    #   until an explicit set/clear action updates it.
    def _redis_official_key(self, project_id, filename):
        safe_filename = self._validate_filename(filename)
        return f"{self._redis_key_prefix}:official:{self._validate_project_id(project_id)}:{safe_filename}"

    # Redis key design for community ranking:
    # - key: {prefix}:community:versions:{project_id}:{filename}
    # - type: ZSET
    # - member: version_id
    # - score: stars
    # - ttl: no TTL. Scores are updated by write/star/unstar and used as the
    #   primary read model for community recommendation queries.
    def _redis_community_versions_key(self, project_id, filename):
        safe_filename = self._validate_filename(filename)
        return f"{self._redis_key_prefix}:community:versions:{self._validate_project_id(project_id)}:{safe_filename}"

    # Redis key design for project-level community leaderboard:
    # - key: {prefix}:community:leaderboard:{project_id}
    # - type: ZSET
    # - member: filename
    # - score: current top stars for that file
    # - ttl: no TTL. Rebuilt on writes/star updates to avoid repeated full scans.
    def _redis_community_leaderboard_key(self, project_id):
        return f"{self._redis_key_prefix}:community:leaderboard:{self._validate_project_id(project_id)}"

    # Redis key design for cached version summary:
    # - key: {prefix}:version:summary:{project_id}:{filename}:{version_id}
    # - type: STRING(JSON)
    # - value: compact version detail used by official/community recommendation APIs
    # - ttl: no TTL. Version snapshots are immutable, and governance/star fields are
    #   refreshed explicitly after related writes.
    def _redis_version_summary_key(self, project_id, filename, version_id):
        safe_filename = self._validate_filename(filename)
        return (
            f"{self._redis_key_prefix}:version:summary:"
            f"{self._validate_project_id(project_id)}:{safe_filename}:{int(version_id)}"
        )

    # Redis key design for ontology exact lookup:
    # - key: {prefix}:ontology:lookup:{project_id}
    # - type: HASH
    # - field: normalized ontology token or filename stem
    # - value: filename
    # - ttl: no TTL. Refreshed explicitly after file writes/deletes.
    def _redis_ontology_lookup_key(self, project_id):
        return f"{self._redis_key_prefix}:ontology:lookup:{self._validate_project_id(project_id)}"

    # Redis key design for ontology candidate catalog:
    # - key: {prefix}:ontology:catalog:{project_id}
    # - type: HASH
    # - field: filename
    # - value: JSON(candidate summary)
    # - ttl: no TTL. Used to avoid filesystem scans for ontology resolution.
    def _redis_ontology_catalog_key(self, project_id):
        return f"{self._redis_key_prefix}:ontology:catalog:{self._validate_project_id(project_id)}"

    # Redis key design for ontology prefix lookup:
    # - key: {prefix}:ontology:prefix:{project_id}:{prefix_token}
    # - type: SET
    # - member: filename
    # - ttl: no TTL. Supports prefix-style fuzzy lookup without scanning files.
    def _redis_ontology_prefix_key(self, project_id, prefix_token):
        return (
            f"{self._redis_key_prefix}:ontology:prefix:"
            f"{self._validate_project_id(project_id)}:{prefix_token}"
        )

    # Redis key design for ontology prefix registry:
    # - key: {prefix}:ontology:prefix_registry:{project_id}
    # - type: SET
    # - member: prefix lookup key name
    # - ttl: no TTL. Lets refresh logic clean old prefix keys precisely.
    def _redis_ontology_prefix_registry_key(self, project_id):
        return f"{self._redis_key_prefix}:ontology:prefix_registry:{self._validate_project_id(project_id)}"

    def _normalize_ontology_lookup(self, value):
        normalized = str(value or "").strip().lower()
        if normalized.endswith(".json"):
            normalized = normalized[:-5]
        return "".join(ch for ch in normalized if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")

    def _build_ontology_candidate(self, project_id, filename):
        safe_filename = self._validate_filename(filename)
        filename_stem = os.path.splitext(os.path.basename(safe_filename))[0]
        ontology_name = ""
        try:
            payload = self.read_version(project_id, safe_filename)
            if isinstance(payload, dict):
                ontology_name = str(payload.get("name", "")).strip()
        except Exception:
            ontology_name = ""
        aliases = []
        for raw in [safe_filename, filename_stem, ontology_name]:
            token = self._normalize_ontology_lookup(raw)
            if token and token not in aliases:
                aliases.append(token)
        return {
            "filename": safe_filename,
            "filename_stem": filename_stem,
            "ontology_name": ontology_name,
            "aliases": aliases,
        }

    def _collect_project_ontology_candidates(self, project_id):
        candidates = []
        for filename in self._list_repo_files(project_id):
            try:
                candidates.append(self._build_ontology_candidate(project_id, filename))
            except Exception:
                continue
        return candidates

    def _refresh_project_ontology_index(self, project_id):
        if self._redis is None:
            return

        try:
            candidates = self._collect_project_ontology_candidates(project_id)
        except Exception as exc:
            logger.warning("Failed to collect ontology candidates for %s: %s", project_id, exc)
            return

        lookup_key = self._redis_ontology_lookup_key(project_id)
        catalog_key = self._redis_ontology_catalog_key(project_id)
        registry_key = self._redis_ontology_prefix_registry_key(project_id)

        alias_to_files = {}
        prefix_to_files = {}
        for candidate in candidates:
            filename = candidate["filename"]
            for alias in candidate.get("aliases", []):
                alias_to_files.setdefault(alias, set()).add(filename)
                for index in range(1, len(alias) + 1):
                    prefix = alias[:index]
                    prefix_to_files.setdefault(prefix, set()).add(filename)

        def _write(client):
            pipeline = client.pipeline()
            old_prefix_keys = client.smembers(registry_key)
            if old_prefix_keys:
                pipeline.delete(*list(old_prefix_keys))
            pipeline.delete(registry_key)
            pipeline.delete(lookup_key)
            pipeline.delete(catalog_key)

            for candidate in candidates:
                pipeline.hset(
                    catalog_key,
                    candidate["filename"],
                    json.dumps(candidate, ensure_ascii=False),
                )

            for alias, filenames in alias_to_files.items():
                if len(filenames) == 1:
                    pipeline.hset(lookup_key, alias, list(filenames)[0])

            for prefix, filenames in prefix_to_files.items():
                prefix_key = self._redis_ontology_prefix_key(project_id, prefix)
                pipeline.sadd(registry_key, prefix_key)
                if filenames:
                    pipeline.sadd(prefix_key, *sorted(filenames))

            pipeline.execute()

        self._redis_call(_write)

    def _get_cached_ontology_candidate(self, project_id, filename):
        def _read(client):
            return client.hget(self._redis_ontology_catalog_key(project_id), self._validate_filename(filename))

        payload = self._redis_call(_read)
        if not payload:
            return None
        try:
            return json.loads(payload)
        except Exception:
            return None

    def _score_ontology_candidate(self, query_token, candidate):
        aliases = candidate.get("aliases", [])
        ontology_name = self._normalize_ontology_lookup(candidate.get("ontology_name", ""))
        filename_stem = self._normalize_ontology_lookup(candidate.get("filename_stem", ""))
        if query_token == ontology_name:
            return 300
        if query_token == filename_stem:
            return 280
        if query_token in aliases:
            return 260
        if ontology_name.startswith(query_token):
            return 220
        if filename_stem.startswith(query_token):
            return 200
        if any(alias.startswith(query_token) for alias in aliases):
            return 180
        joined = " ".join(
            filter(
                None,
                [
                    self._normalize_ontology_lookup(candidate.get("ontology_name", "")),
                    self._normalize_ontology_lookup(candidate.get("filename_stem", "")),
                ],
            )
        )
        if query_token and query_token in joined:
            return 120
        return 0

    def resolve_ontology_query(self, project_id, query):
        normalized_query = self._normalize_ontology_lookup(query)
        if not normalized_query:
            raise ValueError("query 不能为空")

        cached_candidates = []
        exact_filename = self._redis_call(
            lambda client: client.hget(self._redis_ontology_lookup_key(project_id), normalized_query)
        )
        if exact_filename:
            candidate = self._get_cached_ontology_candidate(project_id, exact_filename)
            if candidate:
                return {
                    "project_id": self._validate_project_id(project_id),
                    "query": str(query).strip(),
                    "normalized_query": normalized_query,
                    "matched_by": "redis_exact",
                    "filename": candidate["filename"],
                    "candidate": candidate,
                    "candidates": [candidate],
                }

        prefix_filenames = self._redis_call(
            lambda client: sorted(client.smembers(self._redis_ontology_prefix_key(project_id, normalized_query))),
            default=[],
        )
        for filename in prefix_filenames:
            candidate = self._get_cached_ontology_candidate(project_id, filename)
            if candidate:
                cached_candidates.append(candidate)

        if not cached_candidates:
            catalog_payload = self._redis_call(
                lambda client: client.hgetall(self._redis_ontology_catalog_key(project_id)),
                default={},
            )
            if not catalog_payload and self._redis is not None:
                self._refresh_project_ontology_index(project_id)
                catalog_payload = self._redis_call(
                    lambda client: client.hgetall(self._redis_ontology_catalog_key(project_id)),
                    default={},
                )
            if catalog_payload:
                for raw in catalog_payload.values():
                    try:
                        cached_candidates.append(json.loads(raw))
                    except Exception:
                        continue

        candidates = cached_candidates
        matched_by = "redis_fuzzy"
        if not candidates:
            candidates = self._collect_project_ontology_candidates(project_id)
            matched_by = "fallback_scan"

        scored = []
        for candidate in candidates:
            score = self._score_ontology_candidate(normalized_query, candidate)
            if score > 0:
                scored.append((score, candidate))

        if not scored:
            raise FileNotFoundError(f"未找到与 {query} 匹配的本体")

        scored.sort(
            key=lambda item: (
                int(item[0]),
                len(item[1].get("ontology_name", "") or ""),
                item[1]["filename"],
            ),
            reverse=True,
        )
        top_score = scored[0][0]
        top_candidates = [candidate for score, candidate in scored if score == top_score]
        if len(top_candidates) > 1:
            raise ValueError(
                "存在多个匹配本体: " + ", ".join(f"{item['filename']}({item.get('ontology_name') or 'unknown'})" for item in top_candidates)
            )

        return {
            "project_id": self._validate_project_id(project_id),
            "query": str(query).strip(),
            "normalized_query": normalized_query,
            "matched_by": matched_by,
            "filename": top_candidates[0]["filename"],
            "candidate": top_candidates[0],
            "candidates": top_candidates,
        }

    def _build_version_summary(self, version):
        return {
            "id": version["commit_id"],
            "msg": version["message"],
            "version_id": version["version_id"],
            "filename": version["filename"],
            "object_name": version["object_name"],
            "committer": version["committer"],
            "time": version["time"],
            "changed_files": [],
            "parent_version_ids": version["parent_version_ids"],
            "primary_parent_version_id": version["primary_parent_version_id"],
            "basevision": version["basevision"],
            "currvision": version["currvision"],
            "stars": version["stars"],
            "is_deleted": version["is_deleted"],
            "child_version_ids": version["child_version_ids"],
            "is_root": version["is_root"],
            "is_latest": version["is_latest"],
            "track_tags": version.get("track_tags", ["community"]),
            "is_official_recommended": bool(version.get("is_official_recommended", False)),
            "official_status": version.get("official_status", "none"),
            "official_reason": version.get("official_reason", ""),
            "official_operator": version.get("official_operator", ""),
            "official_at": version.get("official_at", ""),
            "is_highest_star": bool(version.get("is_highest_star", False)),
            "is_community_recommended": bool(version.get("is_community_recommended", False)),
            "community_score": int(version.get("community_score", version["stars"])),
            "community_rank": version.get("community_rank"),
        }

    def _cache_official_entry(self, project_id, filename, entry):
        if not entry:
            self._redis_call(lambda client: client.delete(self._redis_official_key(project_id, filename)))
            return

        def _write(client):
            client.hset(
                self._redis_official_key(project_id, filename),
                mapping={
                    "version_id": int(entry["version_id"]),
                    "reason": entry.get("reason", ""),
                    "operator": entry.get("operator", ""),
                    "updated_at": entry.get("updated_at", ""),
                },
            )

        self._redis_call(_write)

    def _get_cached_official_entry(self, project_id, filename):
        def _read(client):
            return client.hgetall(self._redis_official_key(project_id, filename))

        cached = self._redis_call(_read)
        if not cached:
            return None
        try:
            return {
                "version_id": int(cached["version_id"]),
                "reason": str(cached.get("reason", "")),
                "operator": str(cached.get("operator", "")),
                "updated_at": str(cached.get("updated_at", "")),
            }
        except Exception:
            return None

    def _cache_version_tree(self, project_id, filename, tree):
        if self._redis is None:
            return

        safe_filename = self._validate_filename(filename)
        versions_key = self._redis_community_versions_key(project_id, safe_filename)
        leaderboard_key = self._redis_community_leaderboard_key(project_id)
        current_file_exists = os.path.exists(os.path.join(self._project_path(project_id), safe_filename))

        def _write(client):
            pipeline = client.pipeline()
            pipeline.delete(versions_key)
            for version in tree["versions"]:
                pipeline.set(
                    self._redis_version_summary_key(project_id, safe_filename, version["version_id"]),
                    json.dumps(self._build_version_summary(version), ensure_ascii=False),
                )
                pipeline.zadd(versions_key, {str(int(version["version_id"])): int(version.get("stars", 0))})

            if tree["versions"] and current_file_exists:
                top_version = max(
                    tree["versions"],
                    key=lambda item: (int(item.get("stars", 0)), int(item["version_id"])),
                )
                pipeline.zadd(leaderboard_key, {safe_filename: int(top_version.get("stars", 0))})
            else:
                pipeline.zrem(leaderboard_key, safe_filename)
            pipeline.execute()

        self._redis_call(_write)

    def _get_cached_version_summary(self, project_id, filename, version_id):
        def _read(client):
            return client.get(self._redis_version_summary_key(project_id, filename, version_id))

        payload = self._redis_call(_read)
        if not payload:
            return None
        try:
            return json.loads(payload)
        except Exception:
            return None

    def _refresh_file_cache(self, project_id, filename):
        if self._redis is None:
            return
        try:
            tree = self.get_file_version_tree(project_id, filename)
        except Exception as exc:
            logger.warning("Failed to refresh Redis file cache for %s/%s: %s", project_id, filename, exc)
            return
        self._cache_version_tree(project_id, filename, tree)

    def _now(self):
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _system_actor(self):
        return git.Actor("System", "system@local")

    def _validate_project_id(self, project_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", project_id or ""):
            raise ValueError("project_id 只能包含字母、数字、下划线和短横线")
        return project_id

    def _validate_filename(self, filename):
        normalized = os.path.normpath(filename or "").replace("\\", "/")
        if normalized in {"", ".", ".."}:
            raise ValueError("filename 不能为空")
        if os.path.isabs(filename) or normalized.startswith("../") or "/../" in normalized:
            raise ValueError("filename 非法，禁止路径穿越")
        if normalized.startswith(".git"):
            raise ValueError("filename 非法，禁止写入 .git 目录")
        return normalized

    def _validate_status(self, status):
        if status not in self.ALLOWED_STATUS:
            raise ValueError(f"status 必须是以下之一: {', '.join(sorted(self.ALLOWED_STATUS))}")
        return status

    def _project_path(self, project_id):
        return os.path.join(self.root_dir, self._validate_project_id(project_id))

    def _meta_path(self, project_id):
        return os.path.join(self._project_path(project_id), self.META_FILE)

    def _stars_path(self, project_id):
        safe_project_id = self._validate_project_id(project_id)
        return os.path.join(self._stars_dir, f"{safe_project_id}{self.STARS_FILE_SUFFIX}")

    def build_inference_filename(self, filename):
        safe_filename = self._validate_filename(filename)
        base, ext = os.path.splitext(safe_filename)
        ext = ext or ".json"
        return f"_inference/{base}.probability{ext}"

    def _is_internal_filename(self, filename):
        safe_filename = self._validate_filename(filename)
        return any(safe_filename.startswith(prefix) for prefix in self.INTERNAL_FILE_PREFIXES)

    def _repo_exists(self, project_id):
        return os.path.exists(os.path.join(self._project_path(project_id), ".git"))

    def _get_repo(self, project_id, create=False):
        path = self._project_path(project_id)
        git_dir = os.path.join(path, ".git")
        if not os.path.exists(git_dir):
            if not create:
                raise FileNotFoundError(f"项目 {project_id} 不存在")
            os.makedirs(path, exist_ok=True)
            repo = git.Repo.init(path)
            with open(os.path.join(path, "init.txt"), "w", encoding="utf-8") as f:
                f.write("Project Start")
            repo.git.add(A=True)
            repo.index.commit("System: Initialized", author=self._system_actor())
            return repo
        return git.Repo(path)

    def _abort_pending_revert(self, repo):
        git_dir = repo.git_dir
        if not git_dir:
            return

        revert_head = os.path.join(git_dir, "REVERT_HEAD")
        sequencer_dir = os.path.join(git_dir, "sequencer")
        if not os.path.exists(revert_head) and not os.path.exists(sequencer_dir):
            return

        try:
            repo.git.revert("--abort")
        except Exception:
            pass

    def _write_json_file(self, path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)

    def update_working_copy_fields(self, project_id, filename, fields):
        safe_filename = self._validate_filename(filename)
        if not isinstance(fields, dict) or not fields:
            raise ValueError("fields must be a non-empty object")

        file_path = os.path.join(self._project_path(project_id), safe_filename)
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"文件 {safe_filename} 当前不存在")

        with open(file_path, "r", encoding="utf-8") as f:
            current_data = json.load(f)

        if not isinstance(current_data, dict):
            raise ValueError(f"文件 {safe_filename} 当前内容不是 JSON 对象，无法追加字段")

        current_data.update(fields)
        self._write_json_file(file_path, current_data)
        return current_data

    def _default_meta(self, project_id):
        now = self._now()
        return {
            "project_id": project_id,
            "name": project_id,
            "description": "",
            "status": self.DEFAULT_STATUS,
            "created_at": now,
            "updated_at": now,
            "official_recommendations": {},
            "official_history": {},
        }

    def _load_meta(self, project_id):
        meta = self._default_meta(project_id)
        meta_path = self._meta_path(project_id)
        if os.path.exists(meta_path):
            with open(meta_path, "r", encoding="utf-8") as f:
                stored = json.load(f)
            meta.update(stored)
        if not isinstance(meta.get("official_recommendations"), dict):
            meta["official_recommendations"] = {}
        if not isinstance(meta.get("official_history"), dict):
            meta["official_history"] = {}
        return meta

    def _get_official_recommendation_entry(self, meta, filename):
        safe_filename = self._validate_filename(filename)
        official_map = meta.get("official_recommendations", {})
        if not isinstance(official_map, dict):
            return None

        entry = official_map.get(safe_filename)
        if entry is None:
            return None
        if isinstance(entry, dict):
            version_id = entry.get("version_id")
            if version_id is None:
                return None
            return {
                "version_id": int(version_id),
                "reason": str(entry.get("reason", "")),
                "operator": str(entry.get("operator", "")),
                "updated_at": str(entry.get("updated_at", "")),
            }

        try:
            return {
                "version_id": int(entry),
                "reason": "",
                "operator": "",
                "updated_at": "",
            }
        except (TypeError, ValueError):
            return None

    def _save_meta(self, project_id, meta, repo=None):
        if repo is None:
            repo = self._get_repo(project_id, create=False)
        self._write_json_file(self._meta_path(project_id), meta)
        repo.git.add(self.META_FILE)

    def _load_stars(self, project_id):
        path = self._stars_path(project_id)
        if not os.path.exists(path):
            return {}

        with open(path, "r", encoding="utf-8") as f:
            stored = json.load(f)

        normalized = {}
        for filename, version_map in stored.items():
            if not isinstance(version_map, dict):
                continue
            safe_filename = self._validate_filename(filename)
            normalized[safe_filename] = {}
            for version_id, stars in version_map.items():
                try:
                    normalized[safe_filename][str(int(version_id))] = max(int(stars), 0)
                except (TypeError, ValueError):
                    continue
        return normalized

    def _save_stars(self, project_id, stars):
        self._write_json_file(self._stars_path(project_id), stars)

    def _get_version_stars(self, stars_map, filename, version_id):
        safe_filename = self._validate_filename(filename)
        return int(stars_map.get(safe_filename, {}).get(str(int(version_id)), 0))

    def _normalize_sort_order(self, order):
        normalized = (order or "asc").strip().lower()
        if normalized not in {"asc", "desc"}:
            raise ValueError("order 必须是 asc 或 desc")
        return normalized

    def _normalize_sort_by(self, sort_by):
        normalized = (sort_by or "version").strip().lower()
        if normalized not in {"version", "stars"}:
            raise ValueError("sort_by 仅支持 version 或 stars")
        return normalized

    def _serialize_tree(self, versions, safe_filename):
        latest_version_id = versions[-1]["version_id"] if versions else None
        return {
            "filename": safe_filename,
            "version_count": len(versions),
            "latest_version_id": latest_version_id,
            "latest_commit_id": versions[-1]["commit_id"] if versions else None,
            "root_version_ids": [version["version_id"] for version in versions if version["is_root"]],
            "versions": versions,
        }

    def _filter_and_sort_tree(self, tree, min_stars=0, sort_by="version", order="asc"):
        normalized_min_stars = max(int(min_stars or 0), 0)
        normalized_sort_by = self._normalize_sort_by(sort_by)
        normalized_order = self._normalize_sort_order(order)

        versions = [version.copy() for version in tree["versions"] if int(version.get("stars", 0)) >= normalized_min_stars]
        reverse = normalized_order == "desc"
        if normalized_sort_by == "stars":
            versions.sort(key=lambda version: (int(version.get("stars", 0)), int(version["version_id"])), reverse=reverse)
        else:
            versions.sort(key=lambda version: int(version["version_id"]), reverse=reverse)

        filtered_tree = self._serialize_tree(versions, tree["filename"])
        filtered_tree["filters"] = {
            "min_stars": normalized_min_stars,
            "sort_by": normalized_sort_by,
            "order": normalized_order,
        }
        return filtered_tree

    def _apply_governance_fields(self, project_id, filename, versions):
        if not versions:
            return versions

        safe_filename = self._validate_filename(filename)
        meta = self._load_meta(project_id)
        official_entry = self._get_official_recommendation_entry(meta, safe_filename)
        official_version_id = official_entry.get("version_id") if official_entry else None
        ranked_versions = sorted(
            versions,
            key=lambda item: (int(item.get("stars", 0)), int(item["version_id"])),
            reverse=True,
        )
        highest_star_version_id = ranked_versions[0]["version_id"]
        community_rank_map = {
            version["version_id"]: index + 1
            for index, version in enumerate(ranked_versions)
        }

        enriched_versions = []
        for version in versions:
            enriched = version.copy()
            is_official = official_version_id == version["version_id"]
            stars = int(version.get("stars", 0))
            is_community_recommended = version["version_id"] == highest_star_version_id
            enriched["track_tags"] = ["community"] + (["official"] if is_official else [])
            enriched["is_official_recommended"] = is_official
            enriched["official_status"] = "recommended" if is_official else "none"
            enriched["official_reason"] = official_entry.get("reason", "") if is_official and official_entry else ""
            enriched["official_operator"] = official_entry.get("operator", "") if is_official and official_entry else ""
            enriched["official_at"] = official_entry.get("updated_at", "") if is_official and official_entry else ""
            enriched["is_highest_star"] = is_community_recommended
            enriched["is_community_recommended"] = is_community_recommended
            enriched["community_score"] = stars
            enriched["community_rank"] = community_rank_map.get(version["version_id"])
            enriched_versions.append(enriched)

        return enriched_versions

    def _parse_commit_metadata(self, message):
        metadata = {}
        visible_lines = []
        for line in (message or "").splitlines():
            if ":" not in line:
                visible_lines.append(line)
                continue
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            if key in {
                self.VERSION_META_FILENAME,
                self.VERSION_META_ID,
                self.VERSION_META_BASE,
                self.VERSION_META_OBJECT,
                self.VERSION_META_COMMITTER,
            }:
                metadata[key] = value
                continue
            visible_lines.append(line)
        visible_message = "\n".join(visible_lines).strip()
        return visible_message, metadata

    def _build_commit_message(self, message, filename, version_id, basevision, object_name, committer_name):
        visible_message = (message or "").strip() or "System: version update"
        footer_lines = [
            f"{self.VERSION_META_FILENAME}: {filename}",
            f"{self.VERSION_META_ID}: {version_id}",
            f"{self.VERSION_META_OBJECT}: {object_name}",
            f"{self.VERSION_META_COMMITTER}: {committer_name}",
        ]
        if basevision is not None:
            footer_lines.append(f"{self.VERSION_META_BASE}: {basevision}")
        return f"{visible_message}\n\n" + "\n".join(footer_lines)

    def _file_exists_in_commit(self, commit, filename):
        try:
            commit.tree / filename
            return True
        except KeyError:
            return False

    def _list_repo_files(self, project_id):
        project_path = self._project_path(project_id)
        results = []
        for current_root, dirnames, filenames in os.walk(project_path):
            dirnames[:] = [dirname for dirname in dirnames if dirname != ".git"]
            for filename in filenames:
                abs_path = os.path.join(current_root, filename)
                rel_path = os.path.relpath(abs_path, project_path).replace("\\", "/")
                if rel_path in {"init.txt", self.META_FILE}:
                    continue
                if self._is_internal_filename(rel_path):
                    continue
                results.append(rel_path)
        return sorted(results)

    def _list_historical_files(self, project_id):
        repo = self._get_repo(project_id, create=False)
        filenames = set(self._list_repo_files(project_id))
        for commit in repo.iter_commits():
            _, metadata = self._parse_commit_metadata(commit.message)
            filename = metadata.get(self.VERSION_META_FILENAME)
            if filename and not self._is_internal_filename(filename):
                filenames.add(filename)
        return sorted(filenames)

    def init_project(self, project_id, name=None, description="", status=DEFAULT_STATUS):
        self._validate_status(status)
        existed = self._repo_exists(project_id)
        repo = self._get_repo(project_id, create=True)
        meta = self._load_meta(project_id)
        meta["name"] = name or meta.get("name") or project_id
        meta["description"] = description
        meta["status"] = status
        if not meta.get("created_at"):
            meta["created_at"] = self._now()
        meta["updated_at"] = self._now()

        self._save_meta(project_id, meta, repo)
        commit_id = None
        if repo.is_dirty(untracked_files=True):
            commit = repo.index.commit(
                f"System: 初始化项目 {project_id}",
                author=self._system_actor(),
            )
            commit_id = commit.hexsha
        return {
            "status": "created" if not existed else "updated",
            "commit_id": commit_id,
            "project": self.get_project_info(project_id),
        }

    # 1. 写入 (Write) - 核心：接收 AI 生成的 data 和 message
    def write_version(self, project_id, filename, data, message, agent_name, committer_name, basevision):
        safe_filename = self._validate_filename(filename)
        repo = self._get_repo(project_id, create=True)
        self._abort_pending_revert(repo)
        file_path = os.path.join(self._project_path(project_id), safe_filename)
        tree = self.get_file_version_tree(project_id, safe_filename)
        versions = tree["versions"]
        normalized_basevision = int(basevision)
        if not versions:
            if normalized_basevision != 0:
                raise ValueError(f"文件 {safe_filename} 首次写入时 basevision 必须为 0")
        else:
            if normalized_basevision < 0:
                raise ValueError("basevision 不能小于 0")
            if not any(version["version_id"] == normalized_basevision for version in versions):
                if normalized_basevision != 0:
                    raise FileNotFoundError(f"文件 {safe_filename} 的版本 {normalized_basevision} 不存在")

        next_version_id = (max((version["version_id"] for version in versions), default=0) + 1)
        basevision_for_metadata = normalized_basevision if normalized_basevision > 0 else 0
        full_message = self._build_commit_message(
            message,
            safe_filename,
            next_version_id,
            basevision_for_metadata,
            agent_name,
            committer_name,
        )
        current_data = None
        if os.path.exists(file_path):
            with open(file_path, "r", encoding="utf-8") as f:
                current_data = json.load(f)
        self._write_json_file(file_path, data)

        meta = self._load_meta(project_id)
        meta["status"] = meta.get("status") or self.DEFAULT_STATUS
        meta["updated_at"] = self._now()
        meta["last_agent"] = agent_name
        meta["last_committer"] = committer_name
        meta["last_message"] = message
        meta["last_basevision"] = normalized_basevision
        self._save_meta(project_id, meta, repo)

        repo.git.add(safe_filename)
        author = git.Actor(committer_name, f"{committer_name}@local")
        if repo.is_dirty(untracked_files=True):
            commit = repo.index.commit(
                full_message,
                author=author,
            )
        elif current_data == data and normalized_basevision is not None:
            repo.git.commit(
                "--allow-empty",
                "-m",
                full_message,
                f"--author={committer_name} <{committer_name}@local>",
            )
            commit = repo.head.commit
        else:
            return {"status": "no_change"}

        result = {
            "status": "success",
            "commit_id": commit.hexsha,
            "version_id": next_version_id,
            "basevision": normalized_basevision,
            "currvision": next_version_id,
        }
        self._refresh_file_cache(project_id, safe_filename)
        self._refresh_project_ontology_index(project_id)
        return result

    def delete_version(self, project_id, filename, message, committer_name, agent_name=None):
        safe_filename = self._validate_filename(filename)
        repo = self._get_repo(project_id, create=False)
        self._abort_pending_revert(repo)
        file_path = os.path.join(self._project_path(project_id), safe_filename)
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"文件 {safe_filename} 当前不存在，无法删除")

        tree = self.get_file_version_tree(project_id, safe_filename)
        versions = tree["versions"]
        if not versions:
            raise FileNotFoundError(f"文件 {safe_filename} 不存在历史版本，无法删除")

        latest_version = versions[-1]
        next_version_id = max((version["version_id"] for version in versions), default=0) + 1
        basevision = latest_version["version_id"]
        object_name = (agent_name or latest_version.get("object_name") or os.path.splitext(os.path.basename(safe_filename))[0]).strip()
        full_message = self._build_commit_message(
            (message or "").strip() or f"System: 删除本体 {safe_filename}",
            safe_filename,
            next_version_id,
            basevision,
            object_name,
            committer_name,
        )

        repo.git.rm("--", safe_filename)
        meta = self._load_meta(project_id)
        meta["updated_at"] = self._now()
        meta["last_agent"] = object_name
        meta["last_committer"] = committer_name
        meta["last_message"] = (message or "").strip() or f"System: 删除本体 {safe_filename}"
        meta["last_basevision"] = basevision
        self._save_meta(project_id, meta, repo)

        commit = repo.index.commit(
            full_message,
            author=git.Actor(committer_name, f"{committer_name}@local"),
        )
        result = {
            "status": "success",
            "action": "deleted",
            "filename": safe_filename,
            "commit_id": commit.hexsha,
            "version_id": next_version_id,
            "basevision": basevision,
            "currvision": next_version_id,
        }
        self._refresh_file_cache(project_id, safe_filename)
        self._refresh_project_ontology_index(project_id)
        return result

    def purge_file_history(self, project_id, filename):
        safe_filename = self._validate_filename(filename)
        repo = self._get_repo(project_id, create=False)
        if repo.is_dirty(untracked_files=True):
            raise ValueError("仓库存在未提交改动，请先清理后再执行彻底删除")

        project_path = self._project_path(project_id)
        index_filter_cmd = f'git rm --cached --ignore-unmatch -- "{safe_filename}"'
        try:
            subprocess.run(
                [
                    "git",
                    "filter-branch",
                    "--force",
                    "--index-filter",
                    index_filter_cmd,
                    "--prune-empty",
                    "--tag-name-filter",
                    "cat",
                    "--",
                    "--all",
                ],
                cwd=project_path,
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            err = (exc.stderr or "").strip()
            out = (exc.stdout or "").strip()
            raise RuntimeError(err or out or "彻底删除失败") from exc

        original_refs = repo.git.for_each_ref("--format=%(refname)", "refs/original/").splitlines()
        for ref in original_refs:
            if ref.strip():
                repo.git.update_ref("-d", ref.strip())
        repo.git.reflog("expire", "--expire=now", "--all")
        repo.git.gc("--prune=now", "--aggressive")

        tree = self.get_file_version_tree(project_id, safe_filename)
        return {
            "status": "success",
            "action": "purged",
            "filename": safe_filename,
            "version_count": tree["version_count"],
            "latest_version_id": tree["latest_version_id"],
        }

    # 2. 读取 (Read) - 支持历史版本读取
    def read_version(self, project_id, filename, commit_id=None):
        safe_filename = self._validate_filename(filename)
        repo = self._get_repo(project_id, create=False)
        if commit_id:
            content = repo.git.show(f"{commit_id}:{safe_filename}")
            return json.loads(content)
        path = os.path.join(self._project_path(project_id), safe_filename)
        if not os.path.exists(path):
            raise FileNotFoundError(f"文件 {safe_filename} 不存在")
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    # 3. 日志 (Log) - 查看版本演化
    def get_log(self, project_id, filename=None):
        repo = self._get_repo(project_id, create=False)
        kwargs = {}
        if filename:
            kwargs["paths"] = self._validate_filename(filename)
        return [
            {
                "id": c.hexsha,
                "msg": self._parse_commit_metadata(c.message)[0],
                "object_name": self._parse_commit_metadata(c.message)[1].get(self.VERSION_META_OBJECT, c.author.name),
                "committer": self._parse_commit_metadata(c.message)[1].get(self.VERSION_META_COMMITTER, ""),
                "time": str(c.authored_datetime),
            }
            for c in repo.iter_commits(**kwargs)
        ]

    def get_file_version_tree(self, project_id, filename):
        safe_filename = self._validate_filename(filename)
        repo = self._get_repo(project_id, create=False)
        stars_map = self._load_stars(project_id)
        commits = list(reversed(list(repo.iter_commits(paths=safe_filename))))
        version_id_map = {
            commit.hexsha: index + 1
            for index, commit in enumerate(commits)
        }

        versions = []
        for index, commit in enumerate(commits):
            visible_message, metadata = self._parse_commit_metadata(commit.message)
            version_id = int(metadata.get(self.VERSION_META_ID, version_id_map[commit.hexsha]))
            object_name = metadata.get(self.VERSION_META_OBJECT, commit.author.name)
            committer = metadata.get(self.VERSION_META_COMMITTER, "")
            git_parent_commit_ids = [parent.hexsha for parent in commit.parents]
            explicit_base = metadata.get(self.VERSION_META_BASE)
            if explicit_base not in {None, ""}:
                normalized_explicit_base = int(explicit_base)
                parent_version_ids = [] if normalized_explicit_base <= 0 else [normalized_explicit_base]
            else:
                parent_version_ids = [
                    versions[index - 1]["version_id"]
                ] if index > 0 else []

            primary_parent_version_id = parent_version_ids[0] if parent_version_ids else None
            versions.append(
                {
                    "version_id": version_id,
                    "commit_id": commit.hexsha,
                    "filename": safe_filename,
                    "parent_version_ids": parent_version_ids,
                    "primary_parent_version_id": primary_parent_version_id,
                    "basevision": primary_parent_version_id,
                    "currvision": version_id,
                    "git_parent_commit_ids": git_parent_commit_ids,
                    "message": visible_message,
                    "object_name": object_name,
                    "committer": committer,
                    "stars": self._get_version_stars(stars_map, safe_filename, version_id),
                    "time": str(commit.authored_datetime),
                    "is_deleted": not self._file_exists_in_commit(commit, safe_filename),
                }
            )

        child_map = {version["version_id"]: [] for version in versions}
        for version in versions:
            for parent_version_id in version["parent_version_ids"]:
                if parent_version_id in child_map:
                    child_map[parent_version_id].append(version["version_id"])

        latest_version_id = versions[-1]["version_id"] if versions else None
        for version in versions:
            version["child_version_ids"] = child_map[version["version_id"]]
            version["is_root"] = not version["parent_version_ids"]
            version["is_latest"] = version["version_id"] == latest_version_id

        versions = self._apply_governance_fields(project_id, safe_filename, versions)

        return self._serialize_tree(versions, safe_filename)

    def get_all_version_trees(self, project_id, min_stars=0, sort_by="version", order="asc"):
        self._get_repo(project_id, create=False)
        trees = [
            self._filter_and_sort_tree(
                self.get_file_version_tree(project_id, filename),
                min_stars=min_stars,
                sort_by=sort_by,
                order=order,
            )
            for filename in self._list_repo_files(project_id)
        ]
        return [tree for tree in trees if tree["version_count"] > 0]

    def _find_version_node(self, project_id, version_id, filename=None):
        normalized_version_id = str(version_id)
        if filename:
            safe_filename = self._validate_filename(filename)
            tree = self.get_file_version_tree(project_id, safe_filename)
            for version in tree["versions"]:
                if str(version["version_id"]) == normalized_version_id:
                    return version
            raise FileNotFoundError(f"文件 {safe_filename} 的版本 {version_id} 不存在")

        matched = []
        for historical_filename in self._list_historical_files(project_id):
            tree = self.get_file_version_tree(project_id, historical_filename)
            for version in tree["versions"]:
                if str(version["version_id"]) == normalized_version_id:
                    matched.append(version)
        if len(matched) == 1:
            return matched[0]
        if len(matched) > 1:
            raise ValueError(f"版本 {version_id} 在多个文件中重复，请提供 filename")
        raise FileNotFoundError(f"版本 {version_id} 不存在")

    def get_version_detail(self, project_id, version_id, filename=None):
        version = self._find_version_node(project_id, version_id, filename)
        detail = self.get_commit_detail(project_id, version["commit_id"])
        detail.update(
            {
                "version_id": version["version_id"],
                "filename": version["filename"],
                "object_name": version["object_name"],
                "committer": version["committer"],
                "parent_version_ids": version["parent_version_ids"],
                "primary_parent_version_id": version["primary_parent_version_id"],
                "basevision": version["basevision"],
                "currvision": version["currvision"],
                "stars": version["stars"],
                "is_deleted": version["is_deleted"],
                "child_version_ids": version["child_version_ids"],
                "is_root": version["is_root"],
                "is_latest": version["is_latest"],
                "track_tags": version.get("track_tags", ["community"]),
                "is_official_recommended": bool(version.get("is_official_recommended", False)),
                "official_status": version.get("official_status", "none"),
                "official_reason": version.get("official_reason", ""),
                "official_operator": version.get("official_operator", ""),
                "official_at": version.get("official_at", ""),
                "is_highest_star": bool(version.get("is_highest_star", False)),
                "is_community_recommended": bool(version.get("is_community_recommended", False)),
                "community_score": int(version.get("community_score", version["stars"])),
                "community_rank": version.get("community_rank"),
            }
        )
        return detail

    def read_version_by_id(self, project_id, version_id, filename=None):
        version = self._find_version_node(project_id, version_id, filename)
        if version.get("is_deleted"):
            return {
                "version_id": version["version_id"],
                "commit_id": version["commit_id"],
                "filename": version["filename"],
                "stars": version["stars"],
                "track_tags": version.get("track_tags", ["community"]),
                "is_official_recommended": bool(version.get("is_official_recommended", False)),
                "official_status": version.get("official_status", "none"),
                "official_reason": version.get("official_reason", ""),
                "official_operator": version.get("official_operator", ""),
                "official_at": version.get("official_at", ""),
                "is_highest_star": bool(version.get("is_highest_star", False)),
                "is_community_recommended": bool(version.get("is_community_recommended", False)),
                "community_score": int(version.get("community_score", version["stars"])),
                "community_rank": version.get("community_rank"),
                "deleted": True,
                "data": None,
            }
        return {
            "version_id": version["version_id"],
            "commit_id": version["commit_id"],
            "filename": version["filename"],
            "stars": version["stars"],
            "track_tags": version.get("track_tags", ["community"]),
            "is_official_recommended": bool(version.get("is_official_recommended", False)),
            "official_status": version.get("official_status", "none"),
            "official_reason": version.get("official_reason", ""),
            "official_operator": version.get("official_operator", ""),
            "official_at": version.get("official_at", ""),
            "is_highest_star": bool(version.get("is_highest_star", False)),
            "is_community_recommended": bool(version.get("is_community_recommended", False)),
            "community_score": int(version.get("community_score", version["stars"])),
            "community_rank": version.get("community_rank"),
            "data": self.read_version(project_id, version["filename"], version["commit_id"]),
        }

    def diff_versions(self, project_id, base_version_id, target_version_id, filename=None):
        base_version = self._find_version_node(project_id, base_version_id, filename)
        target_version = self._find_version_node(project_id, target_version_id, filename)
        if base_version["filename"] != target_version["filename"]:
            raise ValueError("版本 Diff 仅支持同一文件")
        return {
            "filename": target_version["filename"],
            "base_version_id": base_version["version_id"],
            "target_version_id": target_version["version_id"],
            "base_commit_id": base_version["commit_id"],
            "target_commit_id": target_version["commit_id"],
            "diff": self.get_diff(
                project_id,
                target_version["filename"],
                base_version["commit_id"],
                target_version["commit_id"],
            ),
        }

    def rollback_version_by_id(self, project_id, version_id, filename=None):
        version = self._find_version_node(project_id, version_id, filename)
        safe_filename = version["filename"]
        tree = self.get_file_version_tree(project_id, safe_filename)
        latest_version = tree["versions"][-1] if tree["versions"] else None
        basevision = latest_version["version_id"] if latest_version else 0

        if version.get("is_deleted"):
            result = self.delete_version(
                project_id,
                safe_filename,
                f"System: rollback to deleted version {version['version_id']}",
                "System",
                version.get("object_name"),
            )
        else:
            target_data = self.read_version(project_id, safe_filename, version["commit_id"])
            result = self.write_version(
                project_id,
                safe_filename,
                target_data,
                f"System: rollback to version {version['version_id']}",
                version.get("object_name") or os.path.splitext(os.path.basename(safe_filename))[0],
                "System",
                basevision,
            )

        result.update(
            {
                "version_id": version["version_id"],
                "filename": safe_filename,
                "target_commit_id": version["commit_id"],
                "target_version_id": version["version_id"],
            }
        )
        return result

    def get_commit_detail(self, project_id, commit_id):
        repo = self._get_repo(project_id, create=False)
        commit = repo.commit(commit_id)
        visible_message, _ = self._parse_commit_metadata(commit.message)
        _, metadata = self._parse_commit_metadata(commit.message)
        return {
            "id": commit.hexsha,
            "msg": visible_message,
            "object_name": metadata.get(self.VERSION_META_OBJECT, commit.author.name),
            "committer": metadata.get(self.VERSION_META_COMMITTER, ""),
            "time": str(commit.authored_datetime),
            "changed_files": sorted(commit.stats.files.keys()),
        }

    # 4. 差异 (Diff) - 为生成规约提供文本素材
    def get_diff(self, project_id, filename, base_commit, target_commit):
        safe_filename = self._validate_filename(filename)
        repo = self._get_repo(project_id, create=False)
        return repo.git.diff(base_commit, target_commit, safe_filename)

    # 5. 回滚 (Rollback) - 强制恢复版本
    def rollback(self, project_id, commit_id):
        repo = self._get_repo(project_id, create=False)
        self._abort_pending_revert(repo)
        repo.git.revert(f"{commit_id}..HEAD", n=True)
        meta = self._load_meta(project_id)
        meta["status"] = "已回滚"
        meta["updated_at"] = self._now()
        self._save_meta(project_id, meta, repo)
        commit = repo.index.commit(
            f"System: 自动回滚至版本 {commit_id}",
            author=self._system_actor(),
        )
        return {"status": "success", "commit_id": commit.hexsha}

    # 6. 项目列表
    def list_projects(self):
        projects = []
        for project_id in os.listdir(self.root_dir):
            project_path = os.path.join(self.root_dir, project_id)
            if not os.path.isdir(project_path):
                continue
            if not os.path.exists(os.path.join(project_path, ".git")):
                continue
            projects.append(self.get_project_info(project_id))
        projects.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        return projects

    # 7. 项目详情
    def get_project_info(self, project_id):
        repo = self._get_repo(project_id, create=False)
        meta = self._load_meta(project_id)
        files = self._list_repo_files(project_id)
        commits = list(repo.iter_commits())
        meta["file_count"] = len(files)
        meta["commit_count"] = len(commits)
        meta["files"] = files
        if commits:
            meta["latest_commit_id"] = commits[0].hexsha
        return meta

    # 8. 项目状态更新
    def update_project_status(self, project_id, status, operator="System"):
        self._validate_status(status)
        repo = self._get_repo(project_id, create=False)
        meta = self._load_meta(project_id)
        meta["status"] = status
        meta["updated_at"] = self._now()
        meta["last_operator"] = operator
        self._save_meta(project_id, meta, repo)

        commit_id = None
        if repo.is_dirty(untracked_files=True):
            commit = repo.index.commit(
                f"System: 项目状态更新为{status}",
                author=git.Actor(operator, f"{operator}@local"),
            )
            commit_id = commit.hexsha
        return {"status": "success", "commit_id": commit_id, "project": self.get_project_info(project_id)}

    # 9. 文件列表
    def list_files(self, project_id):
        self._get_repo(project_id, create=False)
        return self._list_repo_files(project_id)

    # 10. 全部文件版本链
    def get_all_file_timelines(self, project_id):
        self._get_repo(project_id, create=False)
        timelines = []
        for tree in self.get_all_version_trees(project_id):
            history = [
                {
                    "id": version["commit_id"],
                    "version_id": version["version_id"],
                    "msg": version["message"],
                    "object_name": version["object_name"],
                    "committer": version["committer"],
                    "stars": version["stars"],
                    "time": version["time"],
                    "parent_version_ids": version["parent_version_ids"],
                    "primary_parent_version_id": version["primary_parent_version_id"],
                    "basevision": version["basevision"],
                    "currvision": version["currvision"],
                    "is_deleted": version["is_deleted"],
                    "track_tags": version.get("track_tags", ["community"]),
                    "is_official_recommended": bool(version.get("is_official_recommended", False)),
                    "official_status": version.get("official_status", "none"),
                    "official_reason": version.get("official_reason", ""),
                    "official_operator": version.get("official_operator", ""),
                    "official_at": version.get("official_at", ""),
                    "is_highest_star": bool(version.get("is_highest_star", False)),
                    "is_community_recommended": bool(version.get("is_community_recommended", False)),
                    "community_score": int(version.get("community_score", version["stars"])),
                    "community_rank": version.get("community_rank"),
                }
                for version in tree["versions"]
            ]
            timelines.append(
                {
                    "filename": tree["filename"],
                    "version_count": tree["version_count"],
                    "latest_commit_id": tree["latest_commit_id"],
                    "latest_version_id": tree["latest_version_id"],
                    "root_version_ids": tree["root_version_ids"],
                    "history": history,
                }
            )
        return timelines

    def get_official_recommended_version(self, project_id, filename):
        safe_filename = self._validate_filename(filename)
        official_entry = self._get_cached_official_entry(project_id, safe_filename)
        if official_entry:
            summary = self._get_cached_version_summary(project_id, safe_filename, official_entry["version_id"])
            if summary:
                return {
                    "track": "official",
                    "source": "configured",
                    "filename": safe_filename,
                    "recommended_version_id": int(official_entry["version_id"]),
                    "version": summary,
                }
        tree = self.get_file_version_tree(project_id, safe_filename)
        versions = tree["versions"]
        if not versions:
            raise FileNotFoundError(f"文件 {safe_filename} 不存在历史版本")

        official_entry = self._get_official_recommendation_entry(self._load_meta(project_id), safe_filename)
        self._cache_official_entry(project_id, safe_filename, official_entry)
        configured_version_id = official_entry.get("version_id") if official_entry else None

        source = "configured"
        if configured_version_id is not None:
            try:
                version = self._find_version_node(project_id, int(configured_version_id), safe_filename)
            except (FileNotFoundError, ValueError, TypeError):
                version = versions[-1]
                source = "latest_fallback"
        else:
            version = versions[-1]
            source = "latest_fallback"

        detail = self.get_version_detail(project_id, version["version_id"], safe_filename)
        return {
            "track": "official",
            "source": source,
            "filename": safe_filename,
            "recommended_version_id": version["version_id"],
            "version": detail,
        }

    def set_official_recommendation(self, project_id, filename, version_id, operator="System", reason=""):
        safe_filename = self._validate_filename(filename)
        normalized_version_id = int(version_id)
        version = self._find_version_node(project_id, normalized_version_id, safe_filename)
        repo = self._get_repo(project_id, create=False)
        meta = self._load_meta(project_id)
        official_map = meta.setdefault("official_recommendations", {})
        history_map = meta.setdefault("official_history", {})
        previous_entry = self._get_official_recommendation_entry(meta, safe_filename)
        now = self._now()

        official_map[safe_filename] = {
            "version_id": version["version_id"],
            "reason": (reason or "").strip(),
            "operator": (operator or "System").strip() or "System",
            "updated_at": now,
        }
        history_map.setdefault(safe_filename, []).append(
            {
                "action": "set",
                "from_version_id": previous_entry.get("version_id") if previous_entry else None,
                "to_version_id": version["version_id"],
                "version_id": version["version_id"],
                "reason": (reason or "").strip(),
                "operator": (operator or "System").strip() or "System",
                "created_at": now,
            }
        )
        meta["updated_at"] = now
        meta["last_operator"] = (operator or "System").strip() or "System"
        self._save_meta(project_id, meta, repo)

        commit_id = None
        if repo.is_dirty(untracked_files=True):
            commit = repo.index.commit(
                f"System: set official recommendation for {safe_filename} to version {version['version_id']}",
                author=git.Actor(meta["last_operator"], f"{meta['last_operator']}@local"),
            )
            commit_id = commit.hexsha

        self._cache_official_entry(project_id, safe_filename, official_map[safe_filename])
        self._refresh_file_cache(project_id, safe_filename)

        return {
            "status": "success",
            "commit_id": commit_id,
            "track": "official",
            "filename": safe_filename,
            "previous_version_id": previous_entry.get("version_id") if previous_entry else None,
            "recommended_version_id": version["version_id"],
            "version": self.get_version_detail(project_id, version["version_id"], safe_filename),
        }

    def clear_official_recommendation(self, project_id, filename, operator="System", reason=""):
        safe_filename = self._validate_filename(filename)
        repo = self._get_repo(project_id, create=False)
        meta = self._load_meta(project_id)
        official_map = meta.setdefault("official_recommendations", {})
        history_map = meta.setdefault("official_history", {})
        previous_entry = self._get_official_recommendation_entry(meta, safe_filename)
        if not previous_entry:
            return {
                "status": "no_change",
                "track": "official",
                "filename": safe_filename,
                "previous_version_id": None,
                "recommended_version_id": None,
            }

        now = self._now()
        official_map.pop(safe_filename, None)
        history_map.setdefault(safe_filename, []).append(
            {
                "action": "clear",
                "from_version_id": previous_entry.get("version_id"),
                "to_version_id": None,
                "version_id": previous_entry.get("version_id"),
                "reason": (reason or "").strip(),
                "operator": (operator or "System").strip() or "System",
                "created_at": now,
            }
        )
        meta["updated_at"] = now
        meta["last_operator"] = (operator or "System").strip() or "System"
        self._save_meta(project_id, meta, repo)

        commit_id = None
        if repo.is_dirty(untracked_files=True):
            commit = repo.index.commit(
                f"System: clear official recommendation for {safe_filename}",
                author=git.Actor(meta["last_operator"], f"{meta['last_operator']}@local"),
            )
            commit_id = commit.hexsha

        self._cache_official_entry(project_id, safe_filename, None)
        self._refresh_file_cache(project_id, safe_filename)

        return {
            "status": "success",
            "commit_id": commit_id,
            "track": "official",
            "filename": safe_filename,
            "previous_version_id": previous_entry.get("version_id"),
            "recommended_version_id": None,
        }

    def get_official_recommendation_history(self, project_id, filename):
        safe_filename = self._validate_filename(filename)
        meta = self._load_meta(project_id)
        history_map = meta.get("official_history", {})
        if not isinstance(history_map, dict):
            history_map = {}

        history = history_map.get(safe_filename, [])
        if not isinstance(history, list):
            history = []

        return {
            "track": "official",
            "filename": safe_filename,
            "current": self._get_official_recommendation_entry(meta, safe_filename),
            "history": history,
        }

    def get_community_recommended_version(self, project_id, filename):
        safe_filename = self._validate_filename(filename)
        cached_top = self._redis_call(
            lambda client: client.zrevrange(
                self._redis_community_versions_key(project_id, safe_filename),
                0,
                0,
                withscores=True,
            ),
            default=[],
        )
        if cached_top:
            version_id = int(cached_top[0][0])
            summary = self._get_cached_version_summary(project_id, safe_filename, version_id)
            if summary:
                return {
                    "track": "community",
                    "filename": safe_filename,
                    "recommended_version_id": version_id,
                    "stars": int(summary.get("stars", int(cached_top[0][1]))),
                    "version": summary,
                }
        tree = self.get_file_version_tree(project_id, safe_filename)
        versions = tree["versions"]
        if not versions:
            raise FileNotFoundError(f"文件 {safe_filename} 不存在历史版本")

        self._cache_version_tree(project_id, safe_filename, tree)
        version = max(
            versions,
            key=lambda item: (int(item.get("stars", 0)), int(item["version_id"])),
        )
        detail = self.get_version_detail(project_id, version["version_id"], safe_filename)
        return {
            "track": "community",
            "filename": safe_filename,
            "recommended_version_id": version["version_id"],
            "stars": int(version.get("stars", 0)),
            "version": detail,
        }

    def get_community_recommendation_history(self, project_id, filename):
        safe_filename = self._validate_filename(filename)
        cached_versions = self._redis_call(
            lambda client: client.zrevrange(
                self._redis_community_versions_key(project_id, safe_filename),
                0,
                -1,
                withscores=False,
            ),
            default=None,
        )
        if cached_versions:
            cached_history = []
            for version_id in cached_versions:
                summary = self._get_cached_version_summary(project_id, safe_filename, int(version_id))
                if summary is None:
                    cached_history = []
                    break
                cached_history.append(summary)
            if cached_history:
                return {
                    "track": "community",
                    "filename": safe_filename,
                    "history": cached_history,
                }
        tree = self.get_file_version_tree(project_id, safe_filename)
        versions = tree["versions"]
        if not versions:
            raise FileNotFoundError(f"文件 {safe_filename} 不存在历史版本")

        self._cache_version_tree(project_id, safe_filename, tree)
        ranked_versions = sorted(
            versions,
            key=lambda item: (int(item.get("community_score", item.get("stars", 0))), int(item["version_id"])),
            reverse=True,
        )
        return {
            "track": "community",
            "filename": safe_filename,
            "history": [
                self.get_version_detail(project_id, version["version_id"], safe_filename)
                for version in ranked_versions
            ],
        }

    def get_community_leaderboard(self, project_id):
        leaderboard_members = self._redis_call(
            lambda client: client.zrevrange(
                self._redis_community_leaderboard_key(project_id),
                0,
                -1,
                withscores=False,
            ),
            default=None,
        )
        if leaderboard_members:
            leaderboard = []
            for filename in leaderboard_members:
                try:
                    leaderboard.append(self.get_community_recommended_version(project_id, filename))
                except FileNotFoundError:
                    continue
            if leaderboard:
                return {
                    "track": "community",
                    "project_id": self._validate_project_id(project_id),
                    "leaderboard": leaderboard,
                }

        leaderboard = []
        for filename in self._list_repo_files(project_id):
            try:
                recommendation = self.get_community_recommended_version(project_id, filename)
            except FileNotFoundError:
                continue
            leaderboard.append(recommendation)

        leaderboard.sort(
            key=lambda item: (
                int(item["version"].get("community_score", item.get("stars", 0))),
                int(item["recommended_version_id"]),
                item["filename"],
            ),
            reverse=True,
        )
        return {
            "track": "community",
            "project_id": self._validate_project_id(project_id),
            "leaderboard": leaderboard,
        }

    def star_version(self, project_id, version_id, filename=None, increment=1):
        normalized_increment = int(increment)
        if normalized_increment <= 0:
            raise ValueError("increment 必须大于 0")

        version = self._find_version_node(project_id, version_id, filename)
        stars_map = self._load_stars(project_id)
        safe_filename = version["filename"]
        version_key = str(version["version_id"])

        file_stars = stars_map.setdefault(safe_filename, {})
        file_stars[version_key] = int(file_stars.get(version_key, 0)) + normalized_increment
        self._save_stars(project_id, stars_map)
        self._refresh_file_cache(project_id, safe_filename)

        detail = self.get_version_detail(project_id, version["version_id"], safe_filename)
        return {
            "status": "success",
            "project_id": self._validate_project_id(project_id),
            "filename": safe_filename,
            "version_id": version["version_id"],
            "stars": detail["stars"],
            "version": detail,
        }

    def unstar_version(self, project_id, version_id, filename=None, decrement=1):
        normalized_decrement = int(decrement)
        if normalized_decrement <= 0:
            raise ValueError("decrement 必须大于 0")

        version = self._find_version_node(project_id, version_id, filename)
        stars_map = self._load_stars(project_id)
        safe_filename = version["filename"]
        version_key = str(version["version_id"])
        file_stars = stars_map.setdefault(safe_filename, {})
        current_stars = int(file_stars.get(version_key, 0))
        next_stars = max(current_stars - normalized_decrement, 0)

        if next_stars == 0:
            file_stars.pop(version_key, None)
            if not file_stars:
                stars_map.pop(safe_filename, None)
        else:
            file_stars[version_key] = next_stars

        self._save_stars(project_id, stars_map)
        self._refresh_file_cache(project_id, safe_filename)
        detail = self.get_version_detail(project_id, version["version_id"], safe_filename)
        return {
            "status": "success",
            "project_id": self._validate_project_id(project_id),
            "filename": safe_filename,
            "version_id": version["version_id"],
            "stars": detail["stars"],
            "version": detail,
        }

    def list_versions(self, project_id, filename=None, min_stars=0, sort_by="version", order="asc"):
        if filename:
            tree = self.get_file_version_tree(project_id, filename)
            return self._filter_and_sort_tree(tree, min_stars=min_stars, sort_by=sort_by, order=order)
        return {
            "files": self.get_all_version_trees(project_id, min_stars=min_stars, sort_by=sort_by, order=order),
            "filters": {
                "min_stars": max(int(min_stars or 0), 0),
                "sort_by": self._normalize_sort_by(sort_by),
                "order": self._normalize_sort_order(order),
            },
        }
