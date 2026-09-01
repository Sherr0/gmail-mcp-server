import inspect
import unittest
from unittest.mock import Mock

from googleapiclient.errors import HttpError

from gmail.server import GmailService, main


class FakeMessages:
    def __init__(self, failures=None):
        self.failures = failures or set()
        self.modify_calls = []
        self.other_calls = []

    def modify(self, **kwargs):
        self.modify_calls.append(kwargs)
        email_id = kwargs["id"]
        request = Mock()
        if email_id in self.failures:
            response = Mock(status=404, reason="Not Found")
            request.execute.side_effect = HttpError(response, b'{"error":"not found"}')
        else:
            request.execute.return_value = {}
        return request

    def __getattr__(self, name):
        def unexpected(*args, **kwargs):
            self.other_calls.append((name, args, kwargs))
            raise AssertionError(f"Unexpected messages API call: {name}")
        return unexpected


class FakeUsers:
    def __init__(self, messages):
        self._messages = messages

    def messages(self):
        return self._messages


def service_with(messages):
    service = object.__new__(GmailService)
    service.service = Mock()
    service.service.users.return_value = FakeUsers(messages)
    return service


class ArchiveEmailsTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_success_uses_message_modify_and_only_removes_inbox(self):
        messages = FakeMessages()
        result = await service_with(messages).archive_emails(["first", "second"])

        self.assertEqual((result["requested"], result["succeeded"], result["failed"]), (2, 2, 0))
        self.assertEqual(result["results"], [
            {"id": "first", "success": True},
            {"id": "second", "success": True},
        ])
        self.assertEqual(messages.modify_calls, [
            {"userId": "me", "id": "first", "body": {"removeLabelIds": ["INBOX"]}},
            {"userId": "me", "id": "second", "body": {"removeLabelIds": ["INBOX"]}},
        ])
        self.assertEqual(messages.other_calls, [])

    async def test_partial_failure_continues_and_preserves_input_order(self):
        messages = FakeMessages({"second"})
        result = await service_with(messages).archive_emails(["first", "second", "third"])

        self.assertEqual((result["requested"], result["succeeded"], result["failed"]), (3, 2, 1))
        self.assertEqual([item["id"] for item in result["results"]], ["first", "second", "third"])
        self.assertTrue(result["results"][0]["success"])
        self.assertFalse(result["results"][1]["success"])
        self.assertIn("error", result["results"][1])
        self.assertTrue(result["results"][2]["success"])
        self.assertEqual([call["id"] for call in messages.modify_calls], ["first", "second", "third"])

    async def test_invalid_inputs_match_batch_trash_validation(self):
        service = service_with(FakeMessages())
        cases = [
            (None, "email_ids must be a list"),
            ([], "email_ids must contain at least one message ID"),
            (["id"] * 101, "email_ids must contain no more than 100 message IDs"),
            ([""], "every email_id must be a non-empty string"),
            (["   "], "every email_id must be a non-empty string"),
            ([1], "every email_id must be a non-empty string"),
        ]
        for value, message in cases:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, message):
                await service.archive_emails(value)

    async def test_accepts_maximum_batch_size(self):
        messages = FakeMessages()
        result = await service_with(messages).archive_emails([str(index) for index in range(100)])
        self.assertEqual(result["requested"], 100)
        self.assertEqual(result["succeeded"], 100)

    async def test_singular_archive_email_is_unchanged(self):
        messages = FakeMessages()
        result = await service_with(messages).archive_email("one")
        self.assertEqual(result, "Email archived successfully: one")
        self.assertEqual(messages.modify_calls, [
            {"userId": "me", "id": "one", "body": {"removeLabelIds": ["INBOX"]}},
        ])

    def test_tool_registration_and_json_artifact_handler(self):
        source = inspect.getsource(main)
        self.assertIn('name="archive-emails"', source)
        self.assertIn('"minItems": 1', source)
        self.assertIn('"maxItems": 100', source)
        self.assertIn('if name == "archive-emails":', source)
        self.assertIn('artifact={"type": "json", "data": result}', source)


if __name__ == "__main__":
    unittest.main()
