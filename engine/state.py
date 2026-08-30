"""状态机核心（SSOT + 提案确定性合并）。

全部「死板」操作：
- 6 个 JSON 状态文件为机器真值；读写都过 engine/schemas/ 的声明式校验，引擎自身也不写非法数据。
- 提案 = 唯一写入口：信封 schema + 分区规则校验 → 全部通过才落盘（内存事务：先全量合并到副本，
  任一分区报错则整体不写）；落盘阶段再带字节级备份，写失败即整体回滚。
- 幂等：operation_id → canonical hash 登记于 .applied_operations.json；重复跳过、同 id 异内容拒绝。
- 账本：余额永远由流水重算得出，balance_after/current 都不是 AI 可信字段——引擎重算后写回。
- sync 流水线：apply_inbox → verify_state → snapshot <ch>_done（由 cli.cmd_sync 编排）。
"""
from __future__ import annotations

import contextlib
import copy
import json
import re
from pathlib import Path

from . import common, validator

MUTATION_SCHEMA = "novel-studio.state-mutation/v2"
STATE_DIR_NAME = "state"
INBOX_NAME = "inbox"
MARKER_NAME = ".applied_operations.json"
STATE_KEYS = ("current", "entities", "lines", "timeline", "ledger", "synopsis")

CH_RE = re.compile(r"ch_(\d{3,})$")
GUN_ID_RE = re.compile(r"GUN-\d{3,}")
MIS_ID_RE = re.compile(r"MIS-\d{3,}")
NO_MERGE_SUFFIXES = (".draft.json", ".template.json", ".sample.json")

_SCHEMA_CACHE: dict[str, dict] = {}


def _schema(name: str) -> dict:
    if name not in _SCHEMA_CACHE:
        p = Path(__file__).resolve().parent / "schemas" / f"{name}.schema.json"
        _SCHEMA_CACHE[name] = json.loads(p.read_text(encoding="utf-8"))
    return _SCHEMA_CACHE[name]


# ---------------------------------------------------------------------------
# 目录与默认值
# ---------------------------------------------------------------------------
def state_dir(book: Path) -> Path:
    return Path(book) / STATE_DIR_NAME


def inbox_dir(book: Path) -> Path:
    return state_dir(book) / INBOX_NAME


def defaults_for(key: str) -> dict:
    if key == "current":
        return {"time": "", "location": "", "power_level": "", "abilities": "",
                "injury": "", "equipment": "", "assets": "", "situation": "",
                "present_characters": []}
    if key == "entities":
        return {"entries": []}
    if key == "lines":
        return {"foreshadows": [], "misunderstandings": []}
    if key == "timeline":
        return {"events": [], "arcs": []}
    if key == "ledger":
        return {"note": "复式多资源池账本：余额一律由流水重算，禁止手改",
                "pools": {"standard_currency": {"name": "主通货", "unit": "枚", "initial": 0, "current": 0}},
                "transactions": []}
    if key == "synopsis":
        return {"book_logline": "", "chapters": {}}
    raise KeyError(f"未知状态键: {key}")


INBOX_README = """# state/inbox — 提案收件箱（同步官的工位）

一切状态修改从这里进：每章一个 `ch_XXX.json`（schema: engine/schemas/proposal.schema.json，
业务规则见 engine/state.py 分区校验）。processed/ = 已应用的审计记录（永不删改）；
failed/ = 失败提案，就地处修复后重跑 `sync`，引擎自动捡回。

正式提案必须带 operation_id；`*.draft.json`/`*.template.json`/`*.sample.json` 不参与合并，
可放这里当草稿。最小样例（各分区都给了最短合法形状）：

```json
{
  "schema": "novel-studio.state-mutation/v2",
  "chapter": "ch_007",
  "operation_id": "ch_007.syncer.0829a",
  "current": {"location": "青石镇·祠堂", "present_characters": ["沈拓", "村长"]},
  "entities": [{"action": "upsert", "name": "村长", "type": "person",
               "summary": "青石镇村长，玉佩旧案的知情人", "aliases": ["老丈"]},
              {"action": "upsert", "name": "祠堂", "type": "place",
               "summary": "册墙藏十年公册。现状：钥匙轮值周——'现状'写进 summary；status 是枚举"}],
  "lines": [
    {"kind": "foreshadow", "action": "plant", "name": "祠堂牌位下的匣子", "target_ch": 12},
    {"kind": "foreshadow", "action": "resolve", "id": "GUN-003"}
  ],
  "timeline": {"events": [{"time": "次日清晨", "event": "开祠堂"}]},
  "ledger": {"transactions": [{"pool": "standard_currency", "delta": -30,
                  "subject": "香火钱", "counterparty": "祠堂"}]},
  "synopsis": {"title": "祠堂", "text": "沈拓借赔罪进祠堂，瞥见牌位下露出半角匣子。"}
}
```

写提案的纪律：只写增量；事实必须能在本章 final 正文找到出处；不确定就不上账。
status 只许 active/retired（越界整案回滚进 failed/）；"现状/近况"一律并入 summary——upsert 即覆盖，逐章刷新。
"""


def init_state(book: Path) -> int:
    """建目录树 + 播种 6 个状态文件与 inbox 样例（已存在者不动）。返回播种数。"""
    sd = state_dir(book)
    seeded = 0
    for key in STATE_KEYS:
        p = sd / f"{key}.json"
        if not p.exists():
            common.dump_json(p, defaults_for(key))
            seeded += 1
    (sd / INBOX_NAME / "processed").mkdir(parents=True, exist_ok=True)
    (sd / INBOX_NAME / "failed").mkdir(parents=True, exist_ok=True)
    (sd / "snapshots").mkdir(parents=True, exist_ok=True)
    readme = sd / INBOX_NAME / "README.md"
    if not readme.exists():
        readme.write_text(INBOX_README, encoding="utf-8")
    return seeded


# ---------------------------------------------------------------------------
# 读写（双双过 schema）
# ---------------------------------------------------------------------------
def load_state(book: Path, key: str) -> dict:
    p = state_dir(book) / f"{key}.json"
    if not p.exists():
        raise ValueError(f"状态文件缺失: {p.name}（先运行 studio init）")
    data = common.load_json(p)  # 损坏 → 直接抛，不静默兜底
    errors = validator.validate(data, _schema(key))
    if errors:
        raise ValueError(f"{p.name} schema 校验失败: " + "; ".join(errors[:5]))
    return data


def save_state(book: Path, key: str, data: dict) -> None:
    errors = validator.validate(data, _schema(key))
    if errors:
        raise ValueError(f"拒绝写入非法 {key}.json: " + "; ".join(errors[:5]))
    common.dump_json(state_dir(book) / f"{key}.json", data)


def _load_marker(book: Path) -> dict:
    p = state_dir(book) / MARKER_NAME
    if not p.exists():
        return {}
    marker = common.load_json(p)
    if not isinstance(marker, dict):
        raise ValueError(f"{MARKER_NAME} 必须是对象，实际 {type(marker).__name__}")
    return marker


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _chapter_num(ch: str) -> int | None:
    m = CH_RE.search(ch or "")
    return int(m.group(1)) if m else None


def _next_id(items: list[dict], id_key: str, prefix: str) -> str:
    maxn = 0
    for it in items:
        m = re.search(prefix + r"-(\d+)", str(it.get(id_key, "")))
        if m:
            maxn = max(maxn, int(m.group(1)))
    return f"{prefix}-{maxn + 1:03d}"


def _norm_target(value) -> tuple[object, str | None]:
    """target_ch：正整数（引爆章）或 'longline'（长线）。返回 (规范值, 错误)。"""
    if value is None:
        return "longline", None
    if isinstance(value, int) and not isinstance(value, bool):
        return (value, None) if value >= 1 else (value, 'target_ch 必须为正整数章号或 "longline"')
    if isinstance(value, str):
        if value == "longline":
            return "longline", None
        m = re.fullmatch(r"第\s*(\d+)\s*章", value)
        if m:
            return int(m.group(1)), None
    return value, f"target_ch 非法: {value!r}（允许：正整数章号 或 \"longline\"；「第N章」写法自动折算）"


def _index_by(items: list[dict], key: str) -> dict:
    return {str(it.get(key, "")): it for it in items}


# ---------------------------------------------------------------------------
# 提案：校验
# ---------------------------------------------------------------------------
def validate_proposal(proposal, expected_chapter: str | None = None) -> tuple[list[str], dict]:
    """返回 (errors, section_plan)。section_plan 用于 dry-run 展示。"""
    errors: list[str] = []
    plan: dict[str, str] = {}
    if not isinstance(proposal, dict):
        return ["提案必须是 JSON 对象"], plan

    errors.extend(validator.validate(proposal, _schema("proposal")))
    for k in proposal:
        if k.startswith("candidate_"):
            errors.append(f"{k}: 候选字段仅供复核，禁止直接进入合并")
    if proposal.get("_draft"):
        errors.append("这是草稿提案（_draft:true）：复核补全后另存为正式提案再 sync")
    if not proposal.get("operation_id"):
        errors.append("正式提案必须提供 operation_id（幂等身份）")

    chapter = proposal.get("chapter")
    if expected_chapter is not None and chapter != expected_chapter:
        errors.append(f"chapter 与同步目标不一致: {chapter} != {expected_chapter}")

    def _plan(sec, n):
        plan[sec] = f"合并 {sec} × {n}"

    # --- current ---
    cur = proposal.get("current")
    if isinstance(cur, dict):
        _plan("current", len(cur))
        for k in cur:
            if k not in _schema("current")["properties"]:
                errors.append(f"current 含未知字段: {k}")
        pcs = cur.get("present_characters")
        if pcs is not None and (not isinstance(pcs, list) or any(not isinstance(x, str) for x in pcs)):
            errors.append("current.present_characters 必须是字符串数组")

    # --- entities ---
    ents = proposal.get("entities")
    if isinstance(ents, list):
        _plan("entities", len(ents))
        for i, e in enumerate(ents):
            if not isinstance(e, dict):
                errors.append(f"entities[{i}] 必须为对象")
                continue
            if e.get("action", "upsert") not in ("upsert", "retire"):
                errors.append(f"entities[{i}].action 必须为 upsert/retire")
            if not str(e.get("name", "")).strip():
                errors.append(f"entities[{i}].name 必填")

    # --- lines ---
    lines = proposal.get("lines")
    if isinstance(lines, list):
        _plan("lines", len(lines))
        for i, g in enumerate(lines):
            if not isinstance(g, dict):
                errors.append(f"lines[{i}] 必须为对象")
                continue
            kind = g.get("kind")
            if kind not in ("foreshadow", "misunderstanding"):
                errors.append(f"lines[{i}].kind 必须为 foreshadow/misunderstanding")
                continue
            action = g.get("action", "plant")
            if action not in ("plant", "update", "remind", "resolve"):
                errors.append(f"lines[{i}].action 非法: {action}")
            if action == "plant":
                need = ["name"] if kind == "foreshadow" else ["parties", "content"]
                for f in need:
                    if not str(g.get(f, "")).strip():
                        errors.append(f"lines[{i}]（plant {kind}）必须提供 {f}")
                id_re = GUN_ID_RE if kind == "foreshadow" else MIS_ID_RE
                if g.get("id") and not id_re.fullmatch(str(g["id"])):
                    errors.append(f"lines[{i}].id 必须匹配 {id_re.pattern}")
                _, terr = _norm_target(g.get("target_ch"))
                if terr:
                    errors.append(f"lines[{i}]: {terr}")
                pc = g.get("plant_ch")
                if pc is not None and (not isinstance(pc, int) or isinstance(pc, bool) or pc < 1):
                    errors.append(f"lines[{i}].plant_ch 必须为正整数")
            else:
                if not g.get("id"):
                    errors.append(f"lines[{i}]（{action}）必须提供 id")
                if action == "remind" and kind != "foreshadow":
                    errors.append(f"lines[{i}]: remind 只适用于 foreshadow")

    # --- timeline ---
    tl = proposal.get("timeline")
    if isinstance(tl, dict):
        n = len(tl.get("events", []) or []) + len(tl.get("arcs", []) or [])
        _plan("timeline", n)
        for i, ev in enumerate(tl.get("events", []) or []):
            if (not isinstance(ev, dict) or not str(ev.get("time", "")).strip()
                    or not str(ev.get("event", "")).strip()):
                errors.append(f"timeline.events[{i}] 必须含非空 time 与 event")
        for i, a in enumerate(tl.get("arcs", []) or []):
            if not isinstance(a, dict) or not str(a.get("name", "")).strip():
                errors.append(f"timeline.arcs[{i}] 必须含 name")

    # --- ledger ---
    led = proposal.get("ledger")
    if isinstance(led, dict):
        txs = led.get("transactions", []) or []
        _plan("ledger", len(txs))
        pools = led.get("pools")
        if pools is not None and not isinstance(pools, dict):
            errors.append("ledger.pools 必须为对象（新增/修订资源池声明）")
        for i, t in enumerate(txs):
            if not isinstance(t, dict):
                errors.append(f"ledger.transactions[{i}] 必须为对象")
                continue
            if not str(t.get("pool", "")).strip():
                errors.append(f"ledger.transactions[{i}].pool 必填")
            if not str(t.get("subject", "")).strip():
                errors.append(f"ledger.transactions[{i}].subject 必填（记账事由）")
            delta = t.get("delta")
            if not isinstance(delta, int) or isinstance(delta, bool):
                errors.append(f"ledger.transactions[{i}].delta 必须为整数（收入为正/支出为负；拒绝浮点）")
            else:
                ttype = t.get("type")
                if ttype == "income" and delta < 0:
                    errors.append(f"ledger.transactions[{i}]: type=income 但 delta={delta}（正收负支）")
                if ttype == "expense" and delta > 0:
                    errors.append(f"ledger.transactions[{i}]: type=expense 但 delta={delta}（支出必须为负数）")
            if t.get("chapter") is not None and not re.fullmatch(r"ch_\d{3,}", str(t["chapter"])):
                errors.append(f"ledger.transactions[{i}].chapter 须匹配 ch_NNN（缺省用提案章节）")

    # --- synopsis ---
    syn = proposal.get("synopsis")
    if isinstance(syn, dict):
        _plan("synopsis", 1)
        for f in ("text", "title", "book_logline"):
            if f in syn and not isinstance(syn[f], str):
                errors.append(f"synopsis.{f} 必须为字符串")
    return errors, plan


# ---------------------------------------------------------------------------
# 提案：各分区内存合并（只改传入的 data 副本；错误写入 report.errors）
# ---------------------------------------------------------------------------
def _merge_current(state: dict, patch: dict, rep: dict) -> None:
    allowed = set(_schema("current")["properties"])
    for k, v in patch.items():
        if k not in allowed:
            rep["errors"].append(f"current 含未知字段: {k}")
            continue
        if k == "present_characters":
            state["present_characters"] = list(v)
        elif isinstance(v, str):
            state[k] = v
        else:
            rep["errors"].append(f"current.{k} 必须为字符串")
            continue
        rep["updated"].append(f"📍 current.{k} 已更新")


def _merge_entities(state: dict, items: list[dict], rep: dict) -> None:
    idx = _index_by(state["entries"], "name")
    valid_types = set(_schema("entities")["properties"]["entries"]["items"]["properties"]["type"]["enum"])
    for e in items:
        action, name = e.get("action", "upsert"), e["name"]
        if action == "retire":
            ent = idx.get(name)
            if ent is None:
                rep["errors"].append(f"retire 未登记实体「{name}」——不猜测，先补 upsert")
                continue
            ent["status"] = "retired"
            rep["updated"].append(f"🗂️ 实体退役：{name}")
            continue
        ent = idx.get(name)
        etype = e.get("type", "other")
        if etype not in valid_types:
            rep["errors"].append(
                f"实体「{name}」type 非法: {etype}（合法：{'/'.join(sorted(valid_types))}）"
            )
            continue
        if ent is None:
            ent = {"name": name, "type": etype, "aliases": [], "card": "", "summary": "", "status": "active"}
            state["entries"].append(ent)
            idx[name] = ent
        for f in ("type", "card", "summary"):
            if f in e:
                ent[f] = e[f]
        if "status" in e:
            ent["status"] = e["status"]
        if "aliases" in e:
            ent["aliases"] = sorted(set(ent.get("aliases", [])) | {str(a) for a in e["aliases"]})
        rep["updated"].append(f"🗂️ 实体登记/更新：{name}")


def _merge_lines(state: dict, items: list[dict], ch_num: int, rep: dict) -> None:
    buckets = {"foreshadow": state["foreshadows"], "misunderstanding": state["misunderstandings"]}
    for g in items:
        kind, action = g["kind"], g.get("action", "plant")
        arr = buckets[kind]
        prefix = "GUN" if kind == "foreshadow" else "MIS"
        idx = _index_by(arr, "id")
        if action == "plant":
            gid = g.get("id") or _next_id(arr, "id", prefix)
            if gid in idx:
                rep["errors"].append(f"{gid} 已存在，重复 plant 拒绝（改状态请用 update/resolve）")
                continue
            target, terr = _norm_target(g.get("target_ch"))
            if terr:
                rep["errors"].append(f"plant {gid}: {terr}")
                continue
            if kind == "foreshadow":
                arr.append({"id": gid, "name": g["name"], "plant_ch": g.get("plant_ch") or ch_num,
                            "status": "Planted", "target_ch": target, "plan": g.get("plan", "")})
                rep["updated"].append(f"🕸️ 埋设伏笔 {gid}《{g['name']}》→ target {target}")
            else:
                arr.append({"id": gid, "parties": g["parties"], "content": g["content"],
                            "truth": g.get("truth", ""), "level": g.get("level", 1),
                            "target_ch": target, "status": "Active"})
                rep["updated"].append(f"🎭 新误会 {gid}：{g['content'][:30]}")
            idx[gid] = arr[-1]
            continue
        gid = g.get("id")
        ent = idx.get(gid)
        if ent is None:
            rep["errors"].append(f"{action} 目标 {gid} 不存在——台账里没有这条，拒绝猜测")
            continue
        if action == "resolve":
            ent["status"] = "Resolved"
            rep["updated"].append(f"✅ {gid} 已回收/澄清")
        elif action == "remind":
            ent["status"] = "Reminded"
            rep["updated"].append(f"🔔 {gid} 已回唤")
        else:  # update
            allowed = ({"status", "target_ch", "plan", "name"} if kind == "foreshadow"
                       else {"status", "target_ch", "content", "truth", "level", "parties"})
            for k, v in g.items():
                if k in ("kind", "action", "id"):
                    continue
                if k not in allowed:
                    rep["errors"].append(f"update {gid}: 不允许修改字段 {k}")
                    continue
                if k == "target_ch":
                    v, terr = _norm_target(v)
                    if terr:
                        rep["errors"].append(f"update {gid}: {terr}")
                        continue
                if k == "status":
                    enum = ({"Planted", "Reminded", "Resolved"} if kind == "foreshadow"
                            else {"Active", "Resolved"})
                    if v not in enum:
                        rep["errors"].append(f"update {gid}: status 必须 ∈ {sorted(enum)}")
                        continue
                ent[k] = v
            rep["updated"].append(f"🔁 {gid} 已更新")


def _merge_timeline(state: dict, patch: dict, ch: str, rep: dict) -> None:
    existing = {(e.get("time", ""), e.get("event", "")) for e in state["events"]}
    added = skipped = 0
    for ev in patch.get("events", []) or []:
        key = (ev.get("time", ""), ev.get("event", ""))
        if key in existing:
            skipped += 1
            continue
        state["events"].append({"time": ev["time"], "event": ev["event"], "chapter": ch})
        existing.add(key)
        added += 1
    if added:
        rep["updated"].append(f"📜 编年史 +{added} 条")
    if skipped:
        rep["warnings"].append(f"编年史去重跳过 {skipped} 条重复事件")
    arcs = state["arcs"]
    idx = _index_by(arcs, "name")
    for a in patch.get("arcs", []) or []:
        name = a["name"]
        ent = idx.get(name)
        if ent is None:
            ent = {"name": name, "baseline": a.get("baseline") or a.get("stage") or "初始基线",
                   "stage": a.get("stage", ""), "inciting_event": a.get("inciting_event", ""),
                   "ultimate": a.get("ultimate", "")}
            arcs.append(ent)
            idx[name] = ent
            rep["updated"].append(f"🧠 新建成长弧：{name}")
        for f in ("stage", "baseline", "inciting_event", "ultimate"):
            if a.get(f):
                ent[f] = a[f]
        if a.get("strategy"):
            ent["strategy"] = a["strategy"]  # 覆盖式：当前策略只留最新，历史按章归档
            ent.setdefault("strategy_history", []).append({"chapter": ch, "strategy": a["strategy"]})
        rep["updated"].append(f"🧠 {name} 阶段 → {ent.get('stage', '')}")


def _merge_ledger(state: dict, patch: dict, ch: str, rep: dict) -> None:
    pools = state["pools"]
    for pid, p in (patch.get("pools") or {}).items():
        if pid in pools:
            for f in ("name", "unit", "initial"):
                if f in p:
                    pools[pid][f] = p[f]
            rep["warnings"].append(f"资源池 {pid} 声明已修订（余额随重算变化属预期）")
        else:
            pools[pid] = {"name": p.get("name", pid), "unit": p.get("unit", ""),
                          "initial": p.get("initial", 0), "current": p.get("initial", 0)}
            rep["updated"].append(f"💱 新资源池 {pid}（{p.get('name', pid)}）")

    running = {k: v.get("initial", 0) for k, v in pools.items()}
    for i, t in enumerate(state["transactions"]):
        pool = t.get("pool")
        if pool not in running:
            rep["errors"].append(f"既有流水 #{i + 1} 引用未声明池 '{pool}'（数据损坏：修复 ledger 或回滚快照）")
            return
        running[pool] += int(t.get("delta", 0))
        rec = t.get("balance_after")
        if rec is not None and int(rec) != running[pool]:
            rep["errors"].append(
                f"既有流水 #{i + 1} balance_after={rec} 与重算值 {running[pool]} 不符——"
                "状态文件疑似被手改；回滚到最近快照（见 studio snapshot list）")
            return

    for t in patch.get("transactions", []) or []:
        pool = t["pool"]
        if pool not in pools:
            rep["errors"].append(f"流水引用未声明资源池 '{pool}'（先在 ledger.pools 登记）")
            continue
        delta = int(t["delta"])
        running[pool] += delta
        tx = {"chapter": t.get("chapter", ch), "pool": pool, "delta": delta,
              "type": t.get("type") or ("income" if delta >= 0 else "expense"),
              "subject": t["subject"], "balance_after": running[pool]}
        for f in ("counterparty", "note"):
            if t.get(f):
                tx[f] = t[f]
        state["transactions"].append(tx)
        rep["updated"].append(f"💰 {pool} {delta:+} → 余额 {running[pool]}（{tx['subject']}）")

    for k, v in pools.items():
        v["current"] = running.get(k, v.get("initial", 0))
    if patch.get("transactions"):
        rep["updated"].append(f"🧮 余额已从流水全量重算（{len(state['transactions'])} 笔）")


def _merge_synopsis(state: dict, patch: dict, ch: str, rep: dict) -> None:
    if patch.get("book_logline"):
        state["book_logline"] = patch["book_logline"]
        rep["updated"].append("📖 全书 logline 已更新")
    if patch.get("text"):
        chs = state.setdefault("chapters", {})
        prev = chs.get(ch, {})
        if prev.get("source") == "manual" and prev.get("synopsis") and prev["synopsis"] != patch["text"]:
            rep["warnings"].append(f"⚠️ {ch} 已有人工梗概，本次提交覆盖之（旧：{prev['synopsis'][:30]}…）")
        chs[ch] = {"num": _chapter_num(ch) or 0, "title": patch.get("title", prev.get("title", "")),
                   "synopsis": patch["text"], "source": "manual"}
        rep["updated"].append(f"📖 章节梗概已登记（{ch}）")


# ---------------------------------------------------------------------------
# 应用一份提案
# ---------------------------------------------------------------------------
def apply_proposal(book: Path, proposal: dict, expected_chapter: str | None = None,
                   dry_run: bool = False) -> dict:
    rep: dict = {"updated": [], "warnings": [], "errors": [],
                 "chapter": proposal.get("chapter") if isinstance(proposal, dict) else None}
    errors, plan = validate_proposal(proposal, expected_chapter)
    rep["plan"] = plan
    if errors:
        rep["errors"] = errors
        return rep
    if dry_run:
        rep["updated"] = list(plan.values())
        return rep

    ch, op = proposal["chapter"], proposal["operation_id"]
    ch_num = _chapter_num(ch)
    proposal_hash = common.canonical_json_hash({k: v for k, v in proposal.items() if k != "operation_id"})
    try:
        marker = _load_marker(book)
    except (ValueError, OSError) as exc:
        rep["errors"].append(f"幂等登记簿损坏，拒绝合并: {exc}")
        return rep
    if op in marker:
        if marker[op] != proposal_hash:
            rep["errors"].append(f"operation_id {op} 已用于不同内容，拒绝复用（请换新 id）")
        else:
            rep["warnings"].append(f"operation_id {op} 已应用过，跳过（幂等）")
            rep["duplicate"] = True
        return rep
    if proposal_hash in marker.values():
        rep["warnings"].append("相同内容提案（canonical hash）已应用过，跳过（幂等）")
        rep["duplicate"] = True
        return rep

    # 内存事务：先取全部 SSOT 副本，任何损坏 → 拒绝合并
    try:
        data = {key: copy.deepcopy(load_state(book, key)) for key in STATE_KEYS}
    except ValueError as exc:
        rep["errors"].append(f"状态 SSOT 不可用，拒绝合并: {exc}")
        return rep

    if proposal.get("current"):
        _merge_current(data["current"], proposal["current"], rep)
    if proposal.get("entities"):
        _merge_entities(data["entities"], proposal["entities"], rep)
    if proposal.get("lines"):
        _merge_lines(data["lines"], proposal["lines"], ch_num or 0, rep)
    if proposal.get("timeline"):
        _merge_timeline(data["timeline"], proposal["timeline"], ch, rep)
    if proposal.get("ledger"):
        _merge_ledger(data["ledger"], proposal["ledger"], ch, rep)
    if proposal.get("synopsis"):
        _merge_synopsis(data["synopsis"], proposal["synopsis"], ch, rep)
    if rep["errors"]:
        return rep  # 有错 → 一个字节都不写

    # 落盘阶段：全量备份 → 写 → 登记幂等；异常 → 字节级回滚
    sd = state_dir(book)
    paths = {key: sd / f"{key}.json" for key in STATE_KEYS}
    marker_path = sd / MARKER_NAME
    backup = {p: p.read_bytes() for p in list(paths.values()) + ([marker_path] if marker_path.exists() else [])}
    try:
        for key in STATE_KEYS:
            save_state(book, key, data[key])
        marker[op] = proposal_hash
        common.dump_json(marker_path, marker)
    except Exception as exc:
        for p, content in backup.items():
            with contextlib.suppress(OSError):
                p.write_bytes(content)
        rep["errors"].append(f"落盘异常，已整体回滚: {exc}")
        rep["rollback"] = True
    return rep


# ---------------------------------------------------------------------------
# 收件箱批处理（sync 的第一步）
# ---------------------------------------------------------------------------
def _gather(inbox: Path) -> list[Path]:
    if not inbox.exists():
        return []
    return sorted(p for p in inbox.glob("*.json") if not p.name.endswith(NO_MERGE_SUFFIXES))


def _archive(pf: Path, dst: Path) -> Path:
    """移动提案到归档目录；重名自动加 .2/.3… 绝不覆盖审计记录。"""
    dst.mkdir(parents=True, exist_ok=True)
    target = dst / pf.name
    if not target.exists():
        pf.rename(target)
        return target
    for i in range(2, 100):
        cand = dst / f"{pf.stem}.{i}{pf.suffix}"
        if not cand.exists():
            pf.rename(cand)
            return cand
    target = dst / f"{pf.stem}.{common.time_suffix()}{pf.suffix}"
    pf.rename(target)
    return target


def pending_proposals(book: Path) -> list[Path]:
    """inbox 中的未决正式提案（CLI status 共用；不含 drafts/templates/samples）。"""
    return _gather(inbox_dir(book))


def apply_inbox(book: Path, expect_chapter: str | None = None, dry_run: bool = False) -> dict:
    """锁内批处理收件箱。expect_chapter 模式只合并本章提案，其余留在原地；
    非 dry-run 时先从 failed/ 捡回本章提案重试。失败即停（不带着病状态继续合并下一份）。"""
    inbox = inbox_dir(book)
    overall = {"applied": 0, "failed": 0, "duplicates": 0, "skipped": 0, "results": [], "picked_up": False}
    files = _gather(inbox)
    failed_path = inbox / "failed" / f"{expect_chapter}.json" if expect_chapter else None

    with common.file_lock(state_dir(book), name=".state.lock", timeout=30.0):
        if not dry_run and failed_path and failed_path.exists() and not (inbox / f"{expect_chapter}.json").exists():
            failed_path.rename(inbox / f"{expect_chapter}.json")
            overall["picked_up"] = True
            files = _gather(inbox)
        for pf in files:
            result = {"file": pf.name}
            try:
                proposal = common.load_json(pf)
            except (ValueError, OSError) as exc:
                result["errors"] = [f"提案 JSON 解析失败: {exc}"]
                overall["results"].append(result)
                overall["failed"] += 1
                if not dry_run:
                    result["archived_to"] = str(_archive(pf, inbox / "failed"))
                break
            ch = proposal.get("chapter") if isinstance(proposal, dict) else None
            if expect_chapter is not None and ch != expect_chapter:
                result["skipped"] = f"提案章节 {ch} ≠ 同步目标 {expect_chapter}（留在收件箱）"
                overall["skipped"] += 1
                overall["results"].append(result)
                continue
            rep = apply_proposal(book, proposal, expected_chapter=expect_chapter, dry_run=dry_run)
            rep["file"] = pf.name
            overall["results"].append(rep)
            if rep["errors"]:
                overall["failed"] += 1
                if not dry_run:
                    rep["archived_to"] = str(_archive(pf, inbox / "failed"))
                break
            overall["duplicates" if rep.get("duplicate") else "applied"] += 1
            if not dry_run:
                rep["archived_to"] = str(_archive(pf, inbox / "processed"))
    return overall


# ---------------------------------------------------------------------------
# 合并后体检（sync 第二步；M2 的 check 命令复用）
# ---------------------------------------------------------------------------
def verify_state(book: Path) -> list[str]:
    errors: list[str] = []
    data: dict[str, dict] = {}
    for key in STATE_KEYS:
        try:
            data[key] = load_state(book, key)
        except (ValueError, FileNotFoundError) as exc:
            errors.append(str(exc))
    if errors:
        return errors

    led = data["ledger"]
    running = {k: v.get("initial", 0) for k, v in led.get("pools", {}).items()}
    for i, t in enumerate(led.get("transactions", []), 1):
        pool = t.get("pool")
        if pool not in running:
            errors.append(f"流水 #{i} 引用未声明池 '{pool}'")
            continue
        running[pool] += int(t.get("delta", 0))
        if t.get("balance_after") is not None and int(t["balance_after"]) != running[pool]:
            errors.append(f"流水 #{i} balance_after={t['balance_after']} ≠ 重算 {running[pool]}")
    for k, v in led.get("pools", {}).items():
        if int(v.get("current", 0)) != running.get(k, 0):
            errors.append(f"资源池 {k} 声明余额 {v.get('current')} ≠ 流水累计 {running.get(k, 0)}")

    for arr_key, id_re, label in (("foreshadows", GUN_ID_RE, "伏笔"), ("misunderstandings", MIS_ID_RE, "误会")):
        ids = [str(g.get("id", "")) for g in data["lines"].get(arr_key, [])]
        dup = sorted({x for x in ids if ids.count(x) > 1})
        if dup:
            errors.append(f"{label}台账重复编号: {dup}")
        bad = [x for x in ids if not id_re.fullmatch(x)]
        if bad:
            errors.append(f"{label}台账非法编号: {bad[:5]}")
    names = [str(e.get("name", "")) for e in data["entities"].get("entries", [])]
    dup = sorted({x for x in names if names.count(x) > 1})
    if dup:
        errors.append(f"实体注册表重名: {dup}")
    return errors
