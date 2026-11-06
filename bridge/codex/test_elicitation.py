import asyncio
import unittest
from unittest.mock import patch

import bridge
from elicitation import Action, Elicitation, FormError, check_schema, parse, validate
import test_bridge
from test_bridge import message


class ValidationTests(unittest.TestCase):
    def test_numbers_are_typed_and_bounded(self):
        schema = {"type": "integer", "minimum": 0, "maximum": 10}
        self.assertEqual(parse("3", schema, schema), 3)
        for value in ["-1", "11", "2.5", "true", "NaN", "1e9999"]:
            with self.subTest(value=value), self.assertRaises(FormError):
                parse(value, schema, schema)

    def test_string_length_and_formats(self):
        cases = [("email", "person@example.com", "oops"), ("uri", "https://example.com", "oops"),
                 ("date", "2026-11-05", "2026-02-30"),
                 ("date-time", "2026-11-05T12:00:00Z", "2026-11-05T12:00:00")]
        for fmt, good, bad in cases:
            schema = {"type": "string", "format": fmt}
            with self.subTest(fmt=fmt):
                self.assertEqual(parse(good, schema, schema), good)
                with self.assertRaises(FormError):
                    parse(bad, schema, schema)
        schema = {"type": "string", "minLength": 2, "maxLength": 4, "pattern": "^a"}
        self.assertEqual(parse("abc", schema, schema), "abc")
        for value in ["a", "abcde", "bcd"]:
            with self.assertRaises(FormError):
                parse(value, schema, schema)

    def test_nested_extended_forms_and_local_refs(self):
        root = {"type": "object", "$defs": {"port": {"type": "integer", "minimum": 1}},
                "properties": {"config": {"type": "object", "required": ["port"], "additionalProperties": False,
                                          "properties": {"port": {"$ref": "#/$defs/port"}}}}}
        check_schema(root, root)
        self.assertEqual(parse('{"port":8080}', root["properties"]["config"], root), {"port": 8080})
        for text in ['{"port":0}', '{}', '{"port":80,"extra":1}']:
            with self.assertRaises(FormError):
                parse(text, root["properties"]["config"], root)

    def test_unsupported_rules_are_not_silently_ignored(self):
        for schema in [{"type": "object", "if": {}}, {"type": "string", "format": "password"},
                       {"$ref": "https://example.com/schema"}]:
            with self.assertRaises(FormError):
                check_schema(schema, schema)

    def test_multiselect_limits_and_typed_boolean(self):
        schema = {"type": "array", "items": {"enum": ["a", "b"]}, "minItems": 1, "maxItems": 2, "uniqueItems": True}
        self.assertEqual(validate(["a", "b"], schema, schema), ["a", "b"])
        for value in [[], ["a", "a"], ["c"], ["a", "b", "a"]]:
            with self.assertRaises(FormError):
                validate(value, schema, schema)
        for value in [0, 1, "true"]:
            with self.assertRaises(FormError):
                validate(value, {"type": "boolean"}, {})


class FormFlowTests(unittest.IsolatedAsyncioTestCase):
    async def flow(self, schema, answers, mode="form", **params):
        client = unittest.mock.Mock()
        client.safe.side_effect = lambda text: text
        client.say = unittest.mock.AsyncMock()
        client.ask = unittest.mock.AsyncMock(side_effect=answers)
        result = await Elicitation(client).handle({"serverName": "test", "message": "说明", "mode": mode, "requestedSchema": schema, **params})
        return result, client

    async def test_empty_form_requires_explicit_consent(self):
        for answer in [Action.ACCEPT, Action.DECLINE, Action.CANCEL]:
            result, client = await self.flow({"type": "object", "properties": {}}, [answer])
            self.assertEqual(result, {"action": answer.value, "content": {} if answer is Action.ACCEPT else None})
            self.assertEqual(client.ask.await_count, 1)

    async def test_full_form_typed_values_optional_defaults_and_confirmation(self):
        schema = {"type": "object", "required": ["name", "count", "enabled", "tier", "tags"], "properties": {
            "name": {"type": "string", "minLength": 2}, "count": {"type": "integer", "minimum": 1},
            "enabled": {"type": "boolean"}, "tier": {"type": "string", "oneOf": [{"const": "pro", "title": "专业"}]},
            "tags": {"type": "array", "items": {"anyOf": [{"const": "a", "title": "甲"}, {"const": "b", "title": "乙"}]}, "minItems": 1},
            "optional": {"type": "string"}, "region": {"type": "string", "default": "cn"}}}
        answers = [Action.ACCEPT, "alice", "2", False, "pro", ["a", "b"], Action.SKIP, Action.DEFAULT, Action.ACCEPT]
        result, client = await self.flow(schema, answers)
        self.assertEqual(result, {"action": "accept", "content": {"name": "alice", "count": 2, "enabled": False, "tier": "pro", "tags": ["a", "b"], "region": "cn"}})
        self.assertIn("确认提交", client.ask.await_args.args[0])

    async def test_invalid_input_reprompts_without_echoing_raw_value(self):
        schema = {"type": "object", "properties": {"n": {"type": "integer", "minimum": 1}}}
        result, client = await self.flow(schema, [Action.ACCEPT, "private-invalid-value", "-1", "2", Action.ACCEPT])
        self.assertEqual(result["content"], {"n": 2})
        self.assertEqual(client.say.await_count, 2)
        self.assertNotIn("private-invalid-value", str(client.say.await_args_list))

    async def test_rejecting_after_filling_does_not_submit_content(self):
        schema = {"type": "object", "properties": {"name": {"type": "string"}}}
        result, _ = await self.flow(schema, [Action.ACCEPT, "alice", Action.DECLINE])
        self.assertEqual(result, {"action": "decline", "content": None})

    async def test_cancelling_field_does_not_submit_partial_content(self):
        schema = {"type": "object", "properties": {"first": {"type": "string"}, "second": {"type": "string"}}}
        result, _ = await self.flow(schema, [Action.ACCEPT, "alice", Action.CANCEL])
        self.assertEqual(result, {"action": "cancel", "content": None})

    async def test_optional_default_not_applied_without_selection(self):
        schema = {"type": "object", "properties": {"enabled": {"type": "boolean", "default": True}}}
        result, _ = await self.flow(schema, [Action.ACCEPT, Action.SKIP, Action.ACCEPT])
        self.assertEqual(result["content"], {})

    async def test_url_flow_waits_for_user_confirmation(self):
        result, client = await self.flow(None, [Action.ACCEPT], mode="url", url="https://example.com/auth", elicitationId="1")
        self.assertEqual(result, {"action": "accept", "content": None})
        self.assertEqual(client.ask.await_args.kwargs["links"], [("打开网页", "https://example.com/auth")])

    async def test_invalid_url_and_unsupported_schema_are_cancelled(self):
        for url in ["file:///etc/passwd", "javascript:alert(1)", "https://user:password@example.com"]:
            result, client = await self.flow(None, [], mode="url", url=url)
            self.assertEqual(result["action"], "cancel")
            client.ask.assert_not_awaited()
        result, client = await self.flow({"type": "object", "properties": {}, "if": {}}, [])
        self.assertEqual(result["action"], "cancel")
        client.ask.assert_not_awaited()

    async def test_extended_schema_and_alias(self):
        schema = {"type": "object", "properties": {"config": {"type": "object", "properties": {"port": {"type": "integer"}}, "required": ["port"]}}}
        for mode in ["openai/form", "openaiForm"]:
            result, _ = await self.flow(schema, [Action.ACCEPT, '{"port":80}', Action.ACCEPT], mode=mode)
            self.assertEqual(result["content"], {"config": {"port": 80}})


class TelegramFormTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_bridge.ClientTests.asyncSetUp
    asyncTearDown = test_bridge.ClientTests.asyncTearDown
    pair = test_bridge.ClientTests.pair
    async def wait_prompt(self, old_token=None):
        for _ in range(100):
            prompts = [(t, p) for t, p in self.client.prompts.items() if t != old_token and p.get("message_id")]
            if prompts:
                return prompts[0]
            await asyncio.sleep(0)
        self.fail("No prompt appeared")

    async def click(self, token, index):
        prompt = self.client.prompts[token]
        await self.client.callback({"id": "cb", "data": f"prompt:{token}:{index}", "message": message("", mid=prompt["message_id"]), "from": {"id": 123}})

    def event(self, rid, schema):
        return {"id": rid, "method": "mcpServer/elicitation/request", "params": {"threadId": "test-thread", "serverName": "test", "mode": "form", "message": "test form", "requestedSchema": schema}}

    async def test_multiselect_toggles_and_requires_done(self):
        await self.pair()
        task = asyncio.create_task(self.client.ask("多选", [("甲", "a"), ("乙", "b"), ("完成选择", Action.DONE), ("取消", Action.CANCEL)], multiple=True))
        token, _ = await self.wait_prompt()
        await self.click(token, 0)
        await self.click(token, 1)
        await self.click(token, 0)
        self.assertFalse(task.done())
        await self.click(token, 2)
        self.assertEqual(await task, ["b"])

    async def test_parallel_forms_are_serialized(self):
        await self.pair()
        self.client.store.set("thread_id", "test-thread")
        schema = {"type": "object", "properties": {"name": {"type": "string"}}}
        one = asyncio.create_task(self.client.server_request(self.event(101, schema)))
        two = asyncio.create_task(self.client.server_request(self.event(102, schema)))
        token, _ = await self.wait_prompt()
        self.assertEqual(len(self.client.prompts), 1)
        await self.click(token, 0)
        token2, _ = await self.wait_prompt(token)
        await self.client.handle({"update_id": 2, "message": message("alice")})
        token3, _ = await self.wait_prompt(token2)
        await self.click(token3, 0)
        await one
        token4, _ = await self.wait_prompt(token3)
        await self.click(token4, 1)
        await two
        responses = [v for method, v in self.client.rpc.calls if method == "send"]
        self.assertEqual(responses[0], {"id": 101, "result": {"action": "accept", "content": {"name": "alice"}}})
        self.assertEqual(responses[1]["result"], {"action": "decline", "content": None})
        self.assertEqual(self.client.store.queued(), 0)

    async def test_cancel_request_clears_buttons_and_returns_cancel(self):
        await self.pair()
        self.client.store.set("thread_id", "test-thread")
        task = asyncio.create_task(self.client.server_request(self.event(103, {"type": "object", "properties": {}})))
        token, _ = await self.wait_prompt()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.client.prompts, {})
        self.assertIsNone(self.client.current_question)
        self.assertEqual(self.client.rpc.calls[-1][1], {"id": 103, "result": {"action": "cancel", "content": None}})

    async def test_resolved_notification_removes_stale_prompt_without_second_response(self):
        await self.pair()
        self.client.store.set("thread_id", "test-thread")
        loop = asyncio.create_task(self.client.events())
        await self.client.rpc.events.put(self.event(104, {"type": "object", "properties": {}}))
        await self.wait_prompt()
        await self.client.rpc.events.put({"method": "serverRequest/resolved", "params": {"threadId": "test-thread", "requestId": 104}})
        for _ in range(100):
            await asyncio.sleep(0)
            if not self.client.prompts:
                break
        self.assertEqual(self.client.prompts, {})
        self.assertFalse(any(method == "send" for method, _ in self.client.rpc.calls))
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)

    async def test_reply_to_old_question_is_not_used(self):
        await self.pair()
        task = asyncio.create_task(self.client.ask("字段", [], free_text=True))
        await self.wait_prompt()
        old = message("wrong")
        old["reply_to_message"] = {"message_id": 999}
        await self.client.handle({"update_id": 2, "message": old})
        self.assertFalse(task.done())
        await self.client.handle({"update_id": 3, "message": message("correct")})
        self.assertEqual(await task, "correct")

    async def test_answer_and_button_labels_redact_known_credentials(self):
        await self.pair()
        self.client.credentials.append("private-value")
        task = asyncio.create_task(self.client.ask("字段 private-value", [("private-value", "private-value")]))
        token, _ = await self.wait_prompt()
        self.assertNotIn("private-value", str(self.client.tg.sent[-1]))
        with patch.object(self.client.tg, "edit", new=unittest.mock.AsyncMock()) as edit:
            await self.click(token, 0)
            await task
            self.assertNotIn("private-value", str(edit.await_args))


if __name__ == "__main__":
    unittest.main()
