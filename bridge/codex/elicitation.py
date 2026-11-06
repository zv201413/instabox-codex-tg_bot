"""MCP form and URL elicitation over Telegram; no automatic consent."""
from __future__ import annotations

from datetime import date, datetime
from enum import Enum
import json
import math
import re
from urllib.parse import urlsplit


class Action(Enum):
    ACCEPT = "accept"
    DECLINE = "decline"
    CANCEL = "cancel"
    SKIP = "skip"
    DEFAULT = "default"
    DONE = "done"


CONTROLS = [("拒绝表单", Action.DECLINE), ("取消填写", Action.CANCEL)]
CONFIRM = [("确认提交", Action.ACCEPT), *CONTROLS]


class FormError(ValueError):
    pass


def resolved(schema, root):
    if not isinstance(schema, dict):
        raise FormError("不支持此字段的格式")
    for _ in range(16):
        ref = schema.get("$ref")
        if ref is None:
            return schema
        if not isinstance(ref, str) or not ref.startswith("#/"):
            raise FormError("不支持外部字段引用")
        target = root
        try:
            for part in ref[2:].split("/"):
                target = target[part.replace("~1", "/").replace("~0", "~")]
        except (KeyError, TypeError):
            raise FormError("字段引用无效") from None
        if not isinstance(target, dict):
            raise FormError("字段引用无效")
        schema = {**target, **{k: v for k, v in schema.items() if k != "$ref"}}
    raise FormError("字段引用过深")


def check_schema(schema, root, depth=0):
    if depth > 16:
        raise FormError("表单层级过深")
    if isinstance(schema, bool):
        return
    schema = resolved(schema, root)
    supported = {"$schema", "$id", "$defs", "definitions", "title", "description",
                 "type", "properties", "required", "additionalProperties", "items",
                 "enum", "enumNames", "oneOf", "anyOf", "const", "default",
                 "minLength", "maxLength", "pattern", "format", "minimum", "maximum",
                 "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minItems",
                 "maxItems", "uniqueItems", "minProperties", "maxProperties"}
    if set(schema) - supported:
        raise FormError("表单包含暂不支持的校验规则")
    kinds = schema.get("type", [])
    kinds = [kinds] if isinstance(kinds, str) else kinds
    if not isinstance(kinds, list) or set(kinds) - {"string", "number", "integer", "boolean", "array", "object", "null"}:
        raise FormError("不支持此字段的类型")
    if schema.get("format") not in {None, "email", "uri", "date", "date-time"}:
        raise FormError("不支持此字段的格式")
    if schema.get("pattern") is not None:
        try:
            re.compile(schema["pattern"])
        except (re.error, TypeError):
            raise FormError("字段格式规则无效") from None
    properties = schema.get("properties", {})
    if not isinstance(properties, dict) or not isinstance(schema.get("required") or [], list):
        raise FormError("表单字段无效")
    for child in properties.values():
        check_schema(child, root, depth + 1)
    for key in ("items", "additionalProperties"):
        if key in schema:
            check_schema(schema[key], root, depth + 1)
    for key in ("oneOf", "anyOf"):
        if key in schema:
            if not isinstance(schema[key], list) or not schema[key]:
                raise FormError("字段选项无效")
            for child in schema[key]:
                check_schema(child, root, depth + 1)


def validate(value, schema, root, depth=0):
    if depth > 16:
        raise FormError("输入层级过深")
    if isinstance(schema, bool):
        if not schema:
            raise FormError("该值不允许填写")
        return value
    schema = resolved(schema, root)
    for key in ("oneOf", "anyOf"):
        if key in schema:
            matches = 0
            for branch in schema[key]:
                try:
                    validate(value, branch, root, depth + 1)
                    matches += 1
                except FormError:
                    pass
            if not matches or (key == "oneOf" and matches != 1):
                raise FormError("请填写符合选项要求的值")
    if "const" in schema and json.dumps(value) != json.dumps(schema["const"]):
        raise FormError("请填写指定选项")
    if "enum" in schema and not any(json.dumps(value) == json.dumps(x) for x in schema["enum"]):
        raise FormError("请从给出的选项中选择")
    kinds = schema.get("type")
    kinds = [kinds] if isinstance(kinds, str) else kinds
    actual = ("null" if value is None else "boolean" if isinstance(value, bool) else
              "string" if isinstance(value, str) else "object" if isinstance(value, dict) else
              "array" if isinstance(value, list) else "number" if isinstance(value, (int, float)) else "invalid")
    if actual == "number" and isinstance(value, float) and not math.isfinite(value):
        raise FormError("请输入有限的数字")
    if kinds and actual not in kinds and not (actual == "number" and "integer" in kinds and value == int(value)):
        raise FormError("输入类型不正确")
    if actual == "number":
        for key, invalid, label in [("minimum", lambda n: value < n, "不能小于"),
                                     ("maximum", lambda n: value > n, "不能大于"),
                                     ("exclusiveMinimum", lambda n: value <= n, "必须大于"),
                                     ("exclusiveMaximum", lambda n: value >= n, "必须小于")]:
            if schema.get(key) is not None and invalid(schema[key]):
                raise FormError(f"{label} {schema[key]}")
        if schema.get("multipleOf") is not None:
            n = schema["multipleOf"]
            if n <= 0 or not math.isclose(value / n, round(value / n), abs_tol=1e-9):
                raise FormError("数字不符合步长要求")
    elif actual == "string":
        for key, invalid, label in [("minLength", lambda n: len(value) < n, "至少"),
                                     ("maxLength", lambda n: len(value) > n, "最多")]:
            if schema.get(key) is not None and invalid(schema[key]):
                raise FormError(f"请输入{label} {schema[key]} 个字符")
        if schema.get("pattern") is not None and not re.search(schema["pattern"], value):
            raise FormError("文字格式不正确")
        fmt = schema.get("format")
        try:
            if fmt == "email" and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
                raise ValueError()
            if fmt == "uri" and (not urlsplit(value).scheme or re.search(r"\s", value)):
                raise ValueError()
            if fmt == "date":
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    raise ValueError()
                date.fromisoformat(value)
            if fmt == "date-time":
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})", value):
                    raise ValueError()
                datetime.fromisoformat(value.upper())
        except ValueError:
            raise FormError("请输入有效的" + {"email": "邮箱", "uri": "网址", "date": "日期（YYYY-MM-DD）", "date-time": "日期时间（含时区）"}[fmt]) from None
    elif actual == "array":
        if schema.get("minItems") is not None and len(value) < schema["minItems"]:
            raise FormError(f"至少选择 {schema['minItems']} 项")
        if schema.get("maxItems") is not None and len(value) > schema["maxItems"]:
            raise FormError(f"最多选择 {schema['maxItems']} 项")
        if schema.get("uniqueItems") and len({json.dumps(x, sort_keys=True) for x in value}) != len(value):
            raise FormError("请勿重复选择")
        for item in value:
            validate(item, schema.get("items", True), root, depth + 1)
    elif actual == "object":
        if set(schema.get("required") or []) - set(value):
            raise FormError("请填写所有必填项")
        for key, label, invalid in [("minProperties", "字段数量太少", lambda n: len(value) < n),
                                     ("maxProperties", "字段数量太多", lambda n: len(value) > n)]:
            if schema.get(key) is not None and invalid(schema[key]):
                raise FormError(label)
        for key, item in value.items():
            validate(item, schema.get("properties", {}).get(key, schema.get("additionalProperties", True)), root, depth + 1)
    return value


def options(schema, root):
    if isinstance(schema, bool):
        return []
    schema = resolved(schema, root)
    if "enum" in schema:
        labels = schema.get("enumNames") or []
        return [(str(labels[i] if i < len(labels) else x), x) for i, x in enumerate(schema["enum"])]
    branches = schema.get("oneOf", schema.get("anyOf", []))
    if branches and all(isinstance(x, dict) and "const" in x for x in branches):
        return [(str(x.get("title", x["const"])), x["const"]) for x in branches]
    return []


def parse(text, schema, root):
    schema = resolved(schema, root)
    kind = schema.get("type", "string")
    kinds = [kind] if isinstance(kind, str) else kind
    if "null" in kinds and text.strip() == "null":
        value = None
    elif "string" in kinds:
        value = text
    elif "boolean" in kinds:
        normalized = text.strip().lower()
        if normalized not in {"true", "false", "是", "否", "yes", "no"}:
            raise FormError("请选择是或否")
        value = normalized in {"true", "是", "yes"}
    else:
        try:
            def invalid_constant(_):
                raise ValueError()
            value = json.loads(text, parse_constant=invalid_constant)
        except ValueError:
            raise FormError("请输入有效数字" if set(kinds) & {"integer", "number"} else "请输入有效 JSON") from None
    return validate(value, schema, root)


class Elicitation:
    def __init__(self, client):
        self.client = client

    async def handle(self, params):
        heading = "工具请求：" + params.get("serverName", "MCP") + "\n\n" + params.get("message", "请填写表单。")
        mode = params.get("mode", "form")
        if mode == "url":
            url = params.get("url", "")
            try:
                parsed = urlsplit(url)
                valid = parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password
            except ValueError:
                valid = False
            if not valid or self.client.safe(url) != url:
                await self.client.say("工具提供的网页链接无效，已取消此请求。")
                return {"action": "cancel", "content": None}
            answer = await self.client.ask(heading + "\n\n打开网页完成操作后，再点击“已完成”。", [("已完成", Action.ACCEPT), *CONTROLS], links=[("打开网页", url)])
            return {"action": answer.value, "content": None}
        if mode not in {"form", "openai/form", "openaiForm"}:
            await self.client.say("工具请求的表单模式暂不支持，已取消此请求。")
            return {"action": "cancel", "content": None}
        root = params.get("requestedSchema")
        try:
            check_schema(root, root)
            schema = resolved(root, root)
            if schema.get("type", "object") != "object" or not isinstance(schema.get("properties", {}), dict):
                raise FormError("表单必须包含对象字段")
            if len(schema.get("properties", {})) > 32:
                raise FormError("表单字段过多")
            if set(schema.get("required") or []) - set(schema.get("properties", {})):
                raise FormError("表单缺少必填字段定义")
            if any(not isinstance(field, dict) for field in schema.get("properties", {}).values()):
                raise FormError("表单字段必须包含格式定义")
        except (FormError, TypeError, ValueError):
            await self.client.say("此表单格式暂不支持或无效，已取消请求；可以换用普通文字提问。")
            return {"action": "cancel", "content": None}
        action = await self.client.ask(heading + "\n\n" + ("开始后逐项填写，最后确认提交。" if schema.get("properties") else "请确认是否同意此请求。"), [("开始填写", Action.ACCEPT), *CONTROLS] if schema.get("properties") else CONFIRM)
        if action is not Action.ACCEPT:
            return {"action": action.value, "content": None}
        content = {}
        for name, field in schema.get("properties", {}).items():
            field = resolved(field, root)
            required = name in (schema.get("required") or [])
            title = str(field.get("title") or name)
            detail = title + ("（必填）" if required else "（选填）")
            if field.get("description"):
                detail += "\n" + field["description"]
            multiple = field.get("type") == "array" and bool(options(field.get("items", {}), root))
            choices = options(field["items"], root) if multiple else options(field, root)
            if field.get("type") == "boolean":
                choices = [("是", True), ("否", False)]
            if choices:
                detail += "\n" + "\n".join("• " + str(label) for label, _ in choices)
            if multiple:
                detail += "\n可多选；点选后点击“完成选择”。"
                choices.append(("完成选择", Action.DONE))
            else:
                detail += "\n可点选或直接回复文字。"
                if field.get("type") in {"object", "array"}:
                    detail += "复杂字段请回复 JSON。"
            if field.get("default") is not None:
                try:
                    validate(field["default"], field, root)
                    detail += "\n默认值：" + json.dumps(field["default"], ensure_ascii=False)
                    choices.append(("使用默认值", Action.DEFAULT))
                except FormError:
                    pass
            if not required:
                choices.append(("跳过此项", Action.SKIP))
            choices.extend(CONTROLS)
            if len(choices) > 80:
                multiple = False
                choices = [x for x in choices if isinstance(x[1], Action) and x[1] is not Action.DONE]
                detail += "\n选项较多，请直接回复选项值（多选回复 JSON 数组）。"
            while True:
                answer = await self.client.ask(detail, choices, free_text=True, multiple=multiple)
                if answer in (Action.DECLINE, Action.CANCEL):
                    return {"action": answer.value, "content": None}
                if answer is Action.SKIP:
                    break
                if answer is Action.DEFAULT:
                    answer = field["default"]
                try:
                    if isinstance(answer, str) and not any(answer == v for _, v in choices if not isinstance(v, Action)):
                        answer = parse(answer, field, root)
                    else:
                        validate(answer, field, root)
                    content[name] = answer
                    break
                except FormError as error:
                    await self.client.say(str(error) + "，请重新填写“" + title + "”。")
        if not schema.get("properties"):
            try:
                validate(content, schema, root)
            except FormError:
                return {"action": "cancel", "content": None}
            return {"action": "accept", "content": content}
        try:
            validate(content, schema, root)
        except FormError:
            await self.client.say("填写内容未通过表单校验，已取消提交。")
            return {"action": "cancel", "content": None}
        summary = "确认提交给“" + params.get("serverName", "MCP") + "”的内容：\n\n" + json.dumps(content, ensure_ascii=False, indent=2)
        action = await self.client.ask(summary, CONFIRM)
        return {"action": action.value, "content": content if action is Action.ACCEPT else None}
