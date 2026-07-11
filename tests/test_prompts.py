"""System prompt structure, env block, and tool-registry derivations."""

import time

from ddtui.app_support import _git_snapshot, build_env_block
from ddtui.config import (
    POST_SYSTEM_PROMPT,
    SUBAGENT_RESULT_MAX_CHARS,
    SUBAGENT_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
)
from ddtui.tool_schemas import TOOLS
from ddtui.tools import (
    APP_DISPATCHED_TOOLS,
    CONFIRM_TOOLS,
    DIFF_STRIP_TOOLS,
    PARALLEL_SAFE_TOOLS,
    PARENT_TOOL_SCHEMAS,
    SUBAGENT_BLOCKED_TOOLS,
    SUBAGENT_TOOL_SCHEMAS,
    TOOL_FUNCS,
    TOOL_REGISTRY,
)
from tests.conftest import REPO_ROOT


# ── parent prompt ──

def test_prompt_sections():
    for section in [
        "# 角色", "# 行事原则", "# 工具使用",
        "# 任务管理与记忆", "# 输出与验证", "# 注入消息格式",
    ]:
        assert section in SYSTEM_PROMPT, section


def test_prompt_key_rules_present():
    assert "不要用 task_wait 或反复 task_check" in SYSTEM_PROMPT
    assert "结束回复不是放弃任务" in SYSTEM_PROMPT  # anti-polling assurance
    assert "replace_all=true 替换全部出现" in SYSTEM_PROMPT
    assert "覆盖已存在文件前必须先 read_file" in SYSTEM_PROMPT
    assert "行号不是文件内容" in SYSTEM_PROMPT
    assert "回复和思考过程（reasoning）都默认使用中文" in SYSTEM_PROMPT
    assert "文件路径:行号" in SYSTEM_PROMPT
    assert "不要主动 git commit" in SYSTEM_PROMPT
    assert "不要臆造时间或上下文压力" in SYSTEM_PROMPT


def test_all_injection_tags_framed():
    for tag in ["# 历史摘要", "# 探索摘要", "[实时插话]",
                "[Async task notice]", "[Async task complete]",
                "[Subagent result]"]:
        assert tag in SYSTEM_PROMPT, tag


def test_removed_content_stays_removed():
    assert "你可以使用下列tools" not in SYSTEM_PROMPT  # tool-name dump
    assert "提出反方观点" not in SYSTEM_PROMPT          # 5-point critique block
    assert "复制一个备份" not in SYSTEM_PROMPT          # backup anti-pattern
    assert POST_SYSTEM_PROMPT == ""


# ── subagent prompt ──

def test_subagent_prompt_identity_and_contract():
    sp = SUBAGENT_SYSTEM_PROMPT
    assert "你是 ddtui 的子 agent" in sp
    assert f"超过 {SUBAGENT_RESULT_MAX_CHARS} 字符会被截断" in sp
    assert "回复和思考过程（reasoning）都默认使用中文" in sp
    assert "compact_self" in sp


def test_subagent_prompt_matches_its_toolset():
    sp = SUBAGENT_SYSTEM_PROMPT
    # teaches what it has (task/terminal with subagent-specific rules)
    assert "task_start" in sp and "task_pause" in sp
    assert "waiting phase" in sp
    assert "Async task complete" in sp
    assert "terminal_start" in sp
    # teaches explore (subagents own their spans since explore_core)
    assert "explore_start" in sp and "explore_end" in sp
    # never teaches what it lacks
    for absent in ["checkpoint_tool", "spawn_agent", "agent_check", "await_agent"]:
        assert absent not in sp, absent
    assert "你没有 checkpoint" in sp


# ── env block ──

def test_env_block_in_repo():
    env = build_env_block(str(REPO_ROOT), "DeepSeek", "test-model")
    assert "操作系统：" in env
    assert str(REPO_ROOT) in env
    assert "分支 " in env
    # no dirty-state reporting (explicit user request)
    assert "未提交改动" not in env and "工作区干净" not in env
    assert time.strftime("%Y-%m-%d") in env
    assert "DeepSeek / test-model" in env


def test_env_block_outside_repo(tmp_path):
    assert _git_snapshot(str(tmp_path)) is None
    env = build_env_block(str(tmp_path), "X", "y")
    assert "不是 git 仓库" in env


# ── registry derivations ──

def test_registry_covers_all_schemas():
    schema_names = {t["function"]["name"] for t in TOOLS}
    assert schema_names <= set(TOOL_REGISTRY)


def test_derived_sets():
    assert APP_DISPATCHED_TOOLS == frozenset({
        "spawn_agent", "chat_agent", "agent_check", "await_agent",
        "end_agent", "compact_self", "task_pause",
        "explore_start", "explore_end", "explore_cancel",
    })
    assert CONFIRM_TOOLS == frozenset(
        {"write_file", "edit_file", "edit_lines", "multi_edit"}
    )
    assert DIFF_STRIP_TOOLS == frozenset(
        {"edit_file", "edit_lines", "multi_edit"}
    )
    assert SUBAGENT_BLOCKED_TOOLS == frozenset({
        "spawn_agent", "chat_agent", "agent_check", "await_agent", "end_agent",
        "checkpoint_tool", "checkpoint_get", "checkpoint_clear",
        "task_wait",
    })
    assert "read_file" in PARALLEL_SAFE_TOOLS
    assert "write_file" not in PARALLEL_SAFE_TOOLS
    assert "apply_patch" in TOOL_FUNCS


def test_schema_visibility():
    schema_names = {t["function"]["name"] for t in TOOLS}
    parent = {t["function"]["name"] for t in PARENT_TOOL_SCHEMAS}
    sub = {t["function"]["name"] for t in SUBAGENT_TOOL_SCHEMAS}
    assert parent == schema_names - {
        "compact_self", "task_pause", "task_wait", "await_agent",
    }
    assert sub == schema_names - SUBAGENT_BLOCKED_TOOLS


def test_spawn_agent_schema_has_model_effort():
    spawn = next(t for t in TOOLS if t["function"]["name"] == "spawn_agent")
    props = spawn["function"]["parameters"]["properties"]
    assert "model" in props and "effort" in props
    assert "CURRENT provider" in props["model"]["description"]
