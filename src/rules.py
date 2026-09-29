from __future__ import annotations
from .domain import ConflictError, TRIAGE_LEVELS, ValidationError
TITLE='山火事件指挥与离线人员调度'; ENTITY='山火事件'; ID_PREFIX='WF'
SEVERITIES=['low', 'moderate', 'high', 'extreme']; STATES=['reported', 'active', 'contained', 'controlled', 'closed']; TRANSITIONS={'reported': ['active'], 'active': ['contained'], 'contained': ['controlled'], 'controlled': ['closed'], 'closed': []}; TRANSITION_ROLES={'active': ['incident_commander'], 'contained': ['incident_commander'], 'controlled': ['incident_commander'], 'closed': ['incident_commander']}
CREATE_ROLES=set(['field_commander']); RECORD_ROLES=set(['field_commander', 'logistics']); AUDIT_ROLES=set(['incident_commander', 'viewer']); VIEW_ROLES=set(['field_commander', 'incident_commander', 'logistics', 'viewer'])
SEVERITY_WEIGHT={'low': 1.0, 'moderate': 3.0, 'high': 6.0, 'extreme': 9.0}; DEADLINE_HOURS={'low': 72, 'moderate': 24, 'high': 8, 'extreme': 4}; TERMINAL_STATES=set(['closed'])
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))

# ---------------------------------------------------------------------------
# 伤员后送：分诊判定（纯业务规则，不碰存储与HTTP）
# ---------------------------------------------------------------------------
EVACUATION_ENTITY='后送伤员'
# 批次状态：planned=组批完成可重排；confirmed=已确认不可打散；departed=已发车记录不动
BATCH_STATES=['planned', 'confirmed', 'departed']
BATCH_TRANSITIONS={'planned': ['confirmed'], 'confirmed': ['departed'], 'departed': []}
LOCKED_BATCH_STATES=set(['confirmed', 'departed'])
TRIAGE_RANK={level: index for index, level in enumerate(TRIAGE_LEVELS)}
def triage_rank(triage):
    if triage not in TRIAGE_RANK: raise ValidationError("未知分诊等级")
    return TRIAGE_RANK[triage]
def sort_key(casualty):
    # 发车前排序：危重、重伤、轻伤；同等级按现场登记先后（登记序号）
    return (TRIAGE_RANK[casualty["triage"]], casualty["registered_seq"])
def order_for_dispatch(casualties):
    return sorted(casualties, key=sort_key)
def is_upgrade(old_triage, new_triage):
    # 复查调高等级：rank 变小（更危重）才算调高
    return TRIAGE_RANK[new_triage] < TRIAGE_RANK[old_triage]
def validate_recheck(old_triage, new_triage, batch_state):
    if old_triage not in TRIAGE_RANK or new_triage not in TRIAGE_RANK:
        raise ValidationError("未知分诊等级")
    if batch_state == 'departed':
        raise ConflictError("已发车批次记录不能改动")
    if not is_upgrade(old_triage, new_triage):
        raise ConflictError("复查只能调高分诊等级")
def validate_batch_transition(current, target):
    if current not in BATCH_STATES or target not in BATCH_STATES:
        raise ValidationError("未知批次状态")
    if target not in BATCH_TRANSITIONS.get(current, []):
        raise ConflictError(f"批次不能从{current}转换到{target}")
def capacity_gap(casualties, seats, beds):
    """按发车顺序计算座位与床位缺口；床位/座位不足时保留原队列。"""
    n = len(casualties)
    return {"needed": n, "seats": seats, "beds": beds,
            "seat_shortfall": max(0, n - seats), "bed_shortfall": max(0, n - beds)}
def can_lock_batch(casualties, seats, beds):
    # 已确认批次不能打散：确认时运力必须整体够用，缺一个也不确认
    gap = capacity_gap(casualties, seats, beds)
    return gap["seat_shortfall"] == 0 and gap["bed_shortfall"] == 0, gap
