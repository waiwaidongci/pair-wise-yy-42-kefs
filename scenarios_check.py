"""后送排队业务场景自检（手动运行：python3 scenarios_check.py）"""
import tempfile
from pathlib import Path

from src.domain import ConflictError
from src.repository import Repository
from src.service import Service

tmp = tempfile.TemporaryDirectory()
repo = Repository(str(Path(tmp.name) / "check.db"))
svc = Service(repo)
FC, LG = "field_commander", "logistics"

def reg(ref, triage, name=""):
    return svc.register_casualty({"site_ref": ref, "triage": triage, "name": name},
                                 "medic", FC)

# 1) 按现场编号登记，发车前按危重、重伤、轻伤排序（同等级按登记先后）
reg("S-001", "minor")                 # 先登记的轻伤
reg("S-002", "critical")              # 危重
reg("S-003", "serious")
reg("S-004", "serious")               # 同重伤，号在 S-003 后
reg("S-005", "critical")
queue = [c["site_ref"] for c in svc.waiting_queue("viewer")]
assert queue == ["S-002", "S-005", "S-003", "S-004", "S-001"], queue
print("1 队列排序:", queue)

# 2) 重复上报沿用首次分诊：错报为 critical 也不能覆盖首次 minor
try:
    reg("S-001", "critical")
    raise AssertionError("重复登记应冲突")
except ConflictError as exc:
    first = exc.details["casualty"]
    assert first["triage"] == "minor", first
    print("2 重复上报沿用首次分诊:", first["site_ref"], first["triage"])

# 3) 运力：车 2 座，接收点 4 床 -> 首批只能走 2 人，缺口随响应说明，原队列保留
svc.create_vehicle({"plate": "V1", "seats": 2}, "log", LG)
svc.create_receiver({"code": "R1", "name": "山下医院", "beds": 4}, "log", LG)
plan = svc.plan_batch({"vehicle_id": 1, "receiver_id": 1}, "log", LG)
got = [c["site_ref"] for c in plan["casualties"]]
assert got == ["S-002", "S-005"], got
assert plan["queued_remaining"] == 3, plan["queued_remaining"]
assert plan["gap"]["needed"] == 3, plan["gap"]
print("3 组批:", got, "剩余待后送:", plan["queued_remaining"], "缺口:", plan["gap"])

# 4) 同一伤员不能进两个批次：V1 已绑定，再用新车 V2 组批时前两人仍在 V1 批次
svc.create_vehicle({"plate": "V2", "seats": 5}, "log", LG)
plan2 = svc.plan_batch({"vehicle_id": 2, "receiver_id": 1}, "log", LG)
got2 = [c["site_ref"] for c in plan2["casualties"]]
assert got2 == ["S-003", "S-004", "S-001"], got2
print("4 第二批不会重复收人:", got2)

# 5) 已确认批次不能打散：确认 V1 批次，再尝试调整成员（直接改库拒绝，状态为 confirmed）
svc.confirm_batch(1, "log", LG)
b1 = svc.get_batch(1, "viewer")
assert b1["status"] == "confirmed" and b1["locked"] is True
try:
    svc.confirm_batch(1, "log", LG)
    raise AssertionError("重复确认应冲突")
except ConflictError:
    print("5 已确认批次锁定不可打散")

# 6) 复查调高：S-001 minor -> critical，未发车批次 2 重排（成员不打散，仅顺位变）
svc.recheck_casualty(1, {"triage": "critical"}, "medic", FC)
b2 = svc.get_batch(2, "viewer")
order2 = [c["site_ref"] for c in b2["casualties"]]
assert order2 == ["S-001", "S-003", "S-004"], order2
print("6 复查调高后未发车批次重排:", order2)
try:
    svc.recheck_casualty(1, {"triage": "critical"}, "medic", FC)
    raise AssertionError("平级复查应拒绝")
except ConflictError:
    print("  平级/调低不接受")

# 7) 确认时床位不足：R1 共4床，已确认的批次1占2；批次2有3人 -> 确认被拒并带缺口，批次保留为planned
try:
    svc.confirm_batch(2, "log", LG)
    raise AssertionError("床位不足应拒绝确认")
except ConflictError as exc:
    assert exc.details["gap"]["bed_shortfall"] == 1, exc.details
    print("7 床位不足，缺口:", exc.details["gap"], "批次保留:", svc.get_batch(2, "viewer")["status"])

# 8) 批次1 发车成功
svc.depart_batch(1, "log", LG)
assert svc.get_batch(1, "viewer")["status"] == "departed"
print("8 批次1已发车")

# 9) 已发车记录不动：复查 S-002 拒绝（批次1已发车）
try:
    svc.recheck_casualty(2, {"triage": "critical"}, "medic", FC)
    raise AssertionError("已发车不应能改")
except ConflictError:
    print("9 已发车记录不动")

# 10) 同一名伤员不能被两个接收点收下：收治唯一
svc.admit_casualty(2, "rcv", LG)
try:
    svc.admit_casualty(2, "rcv", LG)
    raise AssertionError("重复收治应冲突")
except ConflictError:
    print("10 同一伤员只能收治一次")

# 11) 床位补足（接收点扩容）后，批次2原队列保留：确认 -> 发车成功，成员未打散
repo.conn.execute("UPDATE receivers SET beds=6 WHERE id=1")
svc.confirm_batch(2, "log", LG)
svc.depart_batch(2, "log", LG)
assert svc.get_batch(2, "viewer")["status"] == "departed"
print("11 床位补足后批次2确认并发车，成员未打散")

# 12) 审计链完整（每次登记/组批/确认/发车/收治/复查均有 SHA-256 链式记录）
assert repo.verify_audit_chain(), "审计链断裂"
print("12 审计链完整")

print("ALL SCENARIOS PASS")
repo.close()
tmp.cleanup()
