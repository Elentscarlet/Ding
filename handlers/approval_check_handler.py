#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
审批提交校验处理器（另一个审批模板）。

监听该模板 bpms_instance_change 事件里 type=start（员工提交/发起）的实例，
按配置的规则校验表单，未通过则**直接把当前待办任务拒绝**（驳回）。

为什么要拉实例详情：
  type=start 事件只带 processCode / processInstanceId / staffId / title / url，
  既没有表单内容，也没有 taskId —— 而"拒绝任务"接口必须要 taskId。
  所以流程是：事件 -> 拉实例详情（含表单 + tasks）-> 校验 -> 拒绝。

驳回时的操作人以**当前待办任务的审批人**（tasks[].userId）为准（官方文档也是
从实例详情里取 userId），配置里的 actioner_user_id 只是取不到时的兜底。

两个关键细节：
  1. **时序**：事件可能在审批任务生成之前就到达，所以"拉不到待办任务"会退避重试。
  2. **快速 ack**：拉详情 + 重试可能耗时数秒，因此校验放到线程池里执行、
     handle() 立刻返回，避免钉钉因超时重推事件。
     （代价是进程若正好在此刻被杀，这次校验会丢——属于"失败放行"，
      人工审批人仍会看到这张单子。）
"""

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import List, Optional, Sequence, Tuple

from api import DingTalkApiClient, DingTalkApiError

# 表单里"未找到该字段"时的处理：skip（默认，安全）或 reject
ON_MISSING_SKIP = "skip"
ON_MISSING_REJECT = "reject"

# 待办任务状态：NEW 未启动 / RUNNING 处理中
PENDING_STATUSES = ("RUNNING", "NEW")

DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d",
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S.%fZ",
)

COMPARE_OPS = {
    "gt": (lambda a, b: a > b, "晚于"),
    "ge": (lambda a, b: a >= b, "不早于"),
    "lt": (lambda a, b: a < b, "早于"),
    "le": (lambda a, b: a <= b, "不晚于"),
    "eq": (lambda a, b: a == b, "等于"),
    "ne": (lambda a, b: a != b, "不等于"),
}

NOW_KEYS = ("@now", "now", "现在", "当前时间")

_NUMBER_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)")


# ======================================================================
# 纯函数区（可单测）
# ======================================================================
def index_components(form_component_values) -> dict:
    """按 名称 name / 组件ID id / 别名 bizAlias / 组件类型 componentType 建索引。

    first-wins：同一个键保留最先出现的组件。
    componentType 放在最后，好处是字段 id 万一变了（钉钉的 DDHolidayField-xxxxx
    带随机后缀），配置里直接写 componentType（如 DDHolidayField）仍然能命中。
    """
    index: dict = {}
    for comp in form_component_values or []:
        if not isinstance(comp, dict):
            continue
        for key in ("name", "id", "bizAlias", "componentType"):
            alias = comp.get(key)
            if isinstance(alias, str):
                alias = alias.strip()
            if alias and alias not in index:
                index[alias] = comp
    return index


def extract_form_values(form_component_values) -> dict:
    """把表单组件列表摊平成 {可查询的键: 值}。"""
    values: dict = {}
    for alias, comp in index_components(form_component_values).items():
        value = comp.get("value")
        if isinstance(value, list):
            value = ",".join(str(item) for item in value)
        values[alias] = value
    return values


def parse_ext_value(comp) -> dict:
    """把组件的 extValue 解成 dict。

    extValue 是**JSON 字符串**（且里面可能还有一层被转义的 JSON 字符串，
    例如 extension="{\\"tag\\":\\"调休\\"}"），所以只解外层。
    """
    if not isinstance(comp, dict):
        return {}
    raw = comp.get("extValue")
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def compare_values(left, right, op: str, tolerance: float = 0.0):
    """比较两个值，返回 (是否通过, 可读说明)。

    数值优先（两边都能转成数字就走数值比较，带容差），
    否则退化为去空格的字符串比较。
    """
    left_number = parse_number(left)
    right_number = parse_number(right)
    if left_number is not None and right_number is not None:
        shown = f"{_fmt(left_number)} vs {_fmt(right_number)}"
        if op == "eq":
            return abs(left_number - right_number) <= tolerance, shown
        if op == "ne":
            return abs(left_number - right_number) > tolerance, shown
        if op == "gt":
            return left_number > right_number, shown
        if op == "ge":
            return left_number >= right_number, shown
        if op == "lt":
            return left_number < right_number, shown
        if op == "le":
            return left_number <= right_number, shown
        return False, f"不支持的比较符 {op!r}"

    left_text, right_text = str(left).strip(), str(right).strip()
    shown = f"{left_text!r} vs {right_text!r}"
    if op == "eq":
        return left_text == right_text, shown
    if op == "ne":
        return left_text != right_text, shown
    # 大小比较对非数值没有意义，按不通过处理并说明原因
    return False, f"非数值无法做大小比较：{shown}"


def parse_number(value) -> Optional[float]:
    """把表单值转成数字；转不了返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("，", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    # 容忍 "2天" / "3.5小时" 这类带单位的写法
    match = _NUMBER_RE.match(text)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return None
    return None


def parse_datetime(value, tz: timezone) -> Optional[datetime]:
    """把表单值转成带时区的 datetime。

    兼容：毫秒/秒时间戳、"2026-10-13"、"2026-10-13 09:00[:00]"、
         "2026/10/13"、钉钉详情里的 ISO 形式。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=tz)

    text = str(value).strip()
    if not text:
        return None

    if text.isdigit():
        number = int(text)
        if number > 10 ** 11:          # 毫秒时间戳
            return datetime.fromtimestamp(number / 1000, tz)
        if number > 10 ** 8:           # 秒时间戳
            return datetime.fromtimestamp(number, tz)

    for fmt in DATETIME_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=tz)
        except ValueError:
            continue
    return None


def find_pending_task(detail: dict) -> Optional[dict]:
    """从实例详情的 tasks 里找当前待处理的任务（优先 RUNNING）。"""
    tasks = [t for t in (detail.get("tasks") or []) if isinstance(t, dict)]
    for status in PENDING_STATUSES:
        for task in tasks:
            if str(task.get("status") or "").upper() == status and task.get("taskId") is not None:
                return task
    return None


def evaluate_rules(rules: Sequence, values: dict, tz: timezone,
                   on_missing: str = ON_MISSING_SKIP,
                   components: Optional[dict] = None) -> Tuple[List[str], List[str]]:
    """执行规则校验。

    :param values: extract_form_values() 的结果（键 -> 组件值）
    :param components: index_components() 的结果（键 -> 组件 dict），ext_value_compare 需要
    :return: (未通过原因列表, 需要人工关注的告警列表)
    """
    failures: List[str] = []
    warnings: List[str] = []

    for rule in rules:
        if rule.field not in values:
            message = f"表单里没有找到字段「{rule.field}」，规则未生效"
            if on_missing == ON_MISSING_REJECT:
                failures.append(rule.message or message)
            else:
                warnings.append(message)
            continue

        raw = values.get(rule.field)

        if rule.type == "number_range":
            number = parse_number(raw)
            if number is None:
                failures.append(
                    rule.message
                    or f"字段「{rule.field}」必须是数字（当前值：{_show(raw)}）"
                )
                continue
            if rule.min is not None and number < rule.min:
                failures.append(
                    rule.message
                    or f"字段「{rule.field}」不能小于 {_fmt(rule.min)}（当前值：{_fmt(number)}）"
                )
            elif rule.max is not None and number > rule.max:
                failures.append(
                    rule.message
                    or f"字段「{rule.field}」不能大于 {_fmt(rule.max)}（当前值：{_fmt(number)}）"
                )

        elif rule.type == "ext_value_compare":
            ext = parse_ext_value((components or {}).get(rule.field))
            if not ext:
                failures.append(_with_reason(
                    rule.message,
                    f"组件「{rule.field}」的 extValue 为空或无法解析，无法比对",
                ))
                continue

            absent = [k for k in (rule.left_key, rule.right_key) if k not in ext]
            if absent:
                reason = f"组件「{rule.field}」的 extValue 里缺少字段 {absent}，规则未生效"
                if on_missing == ON_MISSING_REJECT:
                    failures.append(_with_reason(rule.message, reason))
                else:
                    warnings.append(reason)
                continue

            left_value = ext.get(rule.left_key)
            right_value = ext.get(rule.right_key)

            # 配置里显式声明"这些值视为未自定义"时才跳过，默认严格比较
            if rule.ignore_right and str(right_value).strip() in rule.ignore_right:
                warnings.append(
                    f"组件「{rule.field}」的 {rule.right_key}={right_value!r} "
                    f"在 ignore_right 列表内，已跳过一致性校验"
                )
                continue

            passed, shown = compare_values(left_value, right_value, rule.op,
                                          rule.tolerance or 0.0)
            if not passed:
                # 无论有没有自定义 message，都把实际取到的两个值带上，
                # 否则审批意见里只有一句"不一致"，没法追查。
                failures.append(_with_reason(
                    rule.message,
                    f"{rule.left_key}={left_value!r} vs {rule.right_key}={right_value!r}",
                ))

        elif rule.type == "date_compare":
            left = parse_datetime(raw, tz)
            if left is None:
                failures.append(
                    rule.message
                    or f"字段「{rule.field}」不是有效日期（当前值：{_show(raw)}）"
                )
                continue

            compare_key = str(rule.compare_field or "").strip()
            if compare_key.lower() in ("@now", "now") or compare_key in ("现在", "当前时间"):
                right = datetime.now(tz)
                right_label = "当前时间"
            else:
                right = parse_datetime(values.get(compare_key), tz)
                right_label = f"「{compare_key}」"
                if right is None:
                    failures.append(
                        rule.message
                        or f"字段{right_label}不是有效日期"
                           f"（当前值：{_show(values.get(compare_key))}）"
                    )
                    continue

            compare, text = COMPARE_OPS.get(rule.op, (None, rule.op))
            if compare is None or not compare(left, right):
                failures.append(
                    rule.message
                    or f"字段「{rule.field}」必须{text}字段{right_label}"
                )

    return failures, warnings


def _show(value) -> str:
    return "空" if value in (None, "") else repr(value)


def _with_reason(message: str, reason: str) -> str:
    """自定义提示后面附上实际取值，避免审批意见里只有一句"不一致"。"""
    return f"{message}（{reason}）" if message else reason


def _fmt(number: float) -> str:
    return str(int(number)) if float(number).is_integer() else str(number)


# ======================================================================
# 处理器
# ======================================================================
class ApprovalCheckHandler:
    """提交时校验，未通过则驳回。"""

    STATUS_START = "start"

    def __init__(self, cfg, api: DingTalkApiClient, tz: timezone,
                 logger: logging.Logger, max_workers: int = 2):
        """
        :param cfg: AppConfig.approval_check
        :param api: DingTalkApiClient
        :param tz: 日期比较用到的时区
        :param logger: 日志对象
        """
        self.cfg = cfg
        self.api = api
        self.tz = tz
        self.logger = logger
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="approval-check"
        )

    @property
    def process_code(self) -> str:
        return self.cfg.process_code

    def shutdown(self):
        self._executor.shutdown(wait=False)

    # ------------------------------------------------------------------
    async def handle(self, data: dict):
        if (data.get("type") or "") != self.STATUS_START:
            return

        instance_id = data.get("processInstanceId") or ""
        if not instance_id:
            self.logger.warning("审批提交事件缺少 processInstanceId，忽略")
            return

        self.logger.info(
            "审批提交事件 | instanceId=%s | 发起人=%s | 标题=%s | 待校验规则=%d 条",
            instance_id, data.get("staffId"), data.get("title"), len(self.cfg.rules),
        )
        # 校验要拉详情、可能还要等任务生成，放线程池里跑，立刻回 ack
        self._executor.submit(self.check_and_reject, instance_id, data.get("staffId"))

    # ------------------------------------------------------------------
    def check_and_reject(self, instance_id: str, originator: Optional[str] = None):
        try:
            detail, task = self._load_detail_with_task(instance_id)
            if detail is None:
                return

            form = detail.get("formComponentValues")
            # 注意：待办任务已由 _load_detail_with_task 选好并在 task 里
            # （含 taskId 与审批人 userId），这里不要再覆盖成 tasks 列表。
            values = extract_form_values(form)
            failures, warnings = evaluate_rules(
                self.cfg.rules, values, self.tz,
                on_missing=getattr(self.cfg, "on_missing_field", ON_MISSING_SKIP),
                components=index_components(form),
            )
            for message in warnings:
                self.logger.warning("审批校验规则告警 | instanceId=%s | %s", instance_id, message)

            if not failures:
                self.logger.info(
                    "审批提交校验通过 | instanceId=%s | 表单字段=%d 个",
                    instance_id, len(values),
                )
                return

            self.logger.warning(
                "审批提交校验未通过 | instanceId=%s | 发起人=%s | 问题: %s",
                instance_id, detail.get("originatorUserId") or originator,
                "；".join(failures),
            )

            if getattr(self.cfg, "dry_run", False):
                self.logger.warning(
                    "dry_run 已开启：只记录不驳回，这单请人工处理 | instanceId=%s",
                    instance_id,
                )
                return

            self._reject(instance_id, task, failures)

        except Exception as e:
            self.logger.error(
                "审批提交校验处理失败，未自动驳回，请人工处理 | instanceId=%s | %s",
                instance_id, e, exc_info=True,
            )

    # ------------------------------------------------------------------
    def _load_detail_with_task(self, instance_id: str):
        """拉实例详情并等出待办任务；拿不到就返回 (None, None)。"""
        attempts = max(int(self.cfg.task_wait_retries), 1)

        for attempt in range(1, attempts + 1):
            try:
                instance = self.api.get_process_instance(instance_id)
            except DingTalkApiError as e:
                self.logger.error(
                    "获取审批实例详情失败（第 %d/%d 次）| instanceId=%s | %s",
                    attempt, attempts, instance_id, e,
                )
                self._sleep_before_retry(attempt, attempts)
                continue

            detail = instance.get("result") or {}
            status = str(detail.get("status") or "").upper()
            if status and status != "RUNNING":
                self.logger.info(
                    "审批实例已不在审批中（status=%s），跳过校验 | instanceId=%s",
                    status, instance_id,
                )
                return None, None

            task = find_pending_task(detail)
            if task is not None:
                return detail, task

            self.logger.info(
                "暂无待办任务（第 %d/%d 次），稍后重试 | instanceId=%s",
                attempt, attempts, instance_id,
            )
            self._sleep_before_retry(attempt, attempts)

        self.logger.warning(
            "始终未找到待办任务，放弃自动驳回，请人工处理 | instanceId=%s", instance_id
        )
        return None, None

    def _sleep_before_retry(self, attempt: int, attempts: int):
        if attempt < attempts and self.cfg.task_wait_interval > 0:
            time.sleep(self.cfg.task_wait_interval)

    # ------------------------------------------------------------------
    def _reject(self, instance_id: str, task, failures: List[str]):
        # 容错：万一传进来的是 tasks 列表，自己挑出待办任务
        if isinstance(task, list):
            task = find_pending_task({"tasks": task}) or {}
        if not isinstance(task, dict):
            task = {}

        task_id = task.get("taskId")
        if task_id is None:
            self.logger.error(
                "没有可用的 taskId，无法自动驳回，请人工处理 | instanceId=%s | task=%s",
                instance_id, task,
            )
            return

        remark = self.cfg.remark_prefix + "；".join(failures)
        if len(remark) > 500:                      # 审批意见不宜过长
            remark = remark[:497] + "..."

        # 操作人取"当前待办任务的审批人"（tasks[].userId）——
        # 官方文档也是让从实例详情的 userId 取；用别的人容易被 needAuth 拦。
        actioner = str(task.get("userId") or "").strip()
        source = "任务审批人"
        if not actioner:
            actioner = str(getattr(self.cfg, "actioner_user_id", "") or "").strip()
            source = "配置兜底"
            if not actioner:
                self.logger.error(
                    "待办任务没有 userId 且未配置 actioner_user_id，无法自动驳回，请人工处理 | "
                    "instanceId=%s | taskId=%s", instance_id, task_id,
                )
                return
            self.logger.warning(
                "待办任务缺少 userId，改用配置的操作人 | instanceId=%s | 操作人=%s",
                instance_id, actioner,
            )

        self.logger.info(
            "自动驳回 | instanceId=%s | taskId=%s | 操作人=%s(来源=%s) | 意见=%s",
            instance_id, task_id, actioner, source, remark,
        )
        try:
            result = self.api.execute_process_instance(
                process_instance_id=instance_id,
                task_id=task_id,
                result="refuse",
                actioner_user_id=actioner,
                remark=remark,
            )
        except DingTalkApiError as e:
            self.logger.error(
                "自动驳回失败，请人工驳回 | instanceId=%s | 操作人=%s | code=%s | status=%s | %s",
                instance_id, actioner, e.code or e.errcode, e.status_code, e, exc_info=True,
            )
            return

        self.logger.warning(
            "已驳回 | instanceId=%s | 操作人=%s | 返回=%s", instance_id, actioner, result
        )
