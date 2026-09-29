from __future__ import annotations

from typing import Any, Dict, List

from .domain import ValidationError

TITLE = '山火伤员后送排队'

# 发车前排序：危重 > 重伤 > 轻伤，RANK越小越优先
CRITICAL = 'critical'
SERIOUS = 'serious'
MINOR = 'minor'
TRIAGE_LEVELS = [CRITICAL, SERIOUS, MINOR]
TRIAGE_LABELS = {CRITICAL: '危重', SERIOUS: '重伤', MINOR: '轻伤'}
RANK = {level: index for index, level in enumerate(TRIAGE_LEVELS)}


def normalize_triage(value: str) -> str:
    if value not in RANK:
        raise ValidationError("分诊等级必须是critical(危重)/serious(重伤)/minor(轻伤)")
    return value


def retain_first_triage(first_triage: str, repeated_triage: str) -> str:
    # 重复上报沿用首次分诊，后续上报无论是否一致都不改写
    normalize_triage(first_triage)
    normalize_triage(repeated_triage)
    return first_triage


def is_upgrade(old_triage: str, new_triage: str) -> bool:
    normalize_triage(old_triage)
    normalize_triage(new_triage)
    return RANK[new_triage] < RANK[old_triage]


def validate_upgrade(old_triage: str, new_triage: str) -> str:
    # 复查只允许调高等级（轻伤→重伤→危重），调低或持平一律拒绝
    normalize_triage(old_triage)
    normalize_triage(new_triage)
    if RANK[new_triage] >= RANK[old_triage]:
        raise ValidationError(
            f"复查只能调高分诊等级：{TRIAGE_LABELS[old_triage]}不能调为{TRIAGE_LABELS[new_triage]}")
    return new_triage


def queue_sort_key(casualty: Dict[str, Any]):
    # 先按等级，同等级按现场登记先后（id），避免危重被留在队尾
    return (RANK[casualty["current_triage"]], casualty["id"])


def order_queue(casualties: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(casualties, key=queue_sort_key)


def batch_urgency_rank(member_triages: List[str]) -> int:
    # 批次发车轮次：批次内最高优先等级即为批次紧急度
    return min(RANK[t] for t in member_triages)


def seat_gap(waiting_count: int, seats: int) -> int:
    # 车辆座位不足时上不了车的人数；未上车人员继续保留在原队列
    return max(0, waiting_count - seats)


def bed_gap(party_size: int, free_beds: int) -> int:
    # 接收点床位缺口；为0表示可以整体收下
    return max(0, party_size - free_beds)


def describe_gap(kind: str, required: int, available: int) -> str:
    missing = max(0, required - available)
    if kind == "bed":
        return f"接收点床位不足：需要{required}张，空余{available}张，缺口{missing}张，原队列保留"
    return f"车辆座位不足：需要{required}个，可用{available}个，缺口{missing}个，多余人员留队"
