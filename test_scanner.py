"""Guard tests for the three built-in adapters: feeds representative
records, asserts the observable contract (title, cwd, model, tokens, tail).

Run: python -m unittest test_scanner -v
"""

import unittest

from scanner import ClaudeAdapter, CodexAdapter, FileState, OmpAdapter


def feed(adapter, records):
    st = FileState(path="fixture")
    for rec in records:
        adapter.consume(rec, st)
    return st


class OmpTest(unittest.TestCase):
    def test_session(self):
        st = feed(OmpAdapter(), [
            {"type": "title", "title": "", "updatedAt": "2026-09-08T13:57:25Z"},
            {"type": "session", "id": "abc", "timestamp": "2026-09-08T13:57:25Z", "cwd": "C:\\work"},
            {"type": "message", "timestamp": "2026-09-08T13:59:00Z",
             "message": {"role": "user", "content": [{"type": "text", "text": "build me a dashboard"}]}},
            {"type": "message", "timestamp": "2026-09-08T14:00:00Z",
             "message": {"role": "assistant", "model": "kimi-k3",
                         "usage": {"totalTokens": 12345}, "stopReason": "toolUse",
                         "content": [{"type": "thinking", "thinking": "secret"},
                                     {"type": "text", "text": "on it"},
                                     {"type": "toolCall", "name": "write"}]}},
        ])
        self.assertEqual(st.session_id, "abc")
        self.assertEqual(st.cwd, "C:\\work")
        self.assertEqual(st.title, "build me a dashboard")  # falls back to first user text
        self.assertEqual(st.model, "kimi-k3")               # model/usage live inside message
        self.assertEqual(st.tokens, 12345)
        self.assertEqual(st.hint, "busy")
        self.assertEqual([(e.who, e.text) for e in st.tail],
                         [("user", "build me a dashboard"), ("think", "secret"),
                          ("assistant", "on it"), ("tool", "write")])


class ClaudeTest(unittest.TestCase):
    def test_session(self):
        st = feed(ClaudeAdapter(), [
            {"type": "summary", "summary": "Fix the flaky test"},
            {"type": "user", "timestamp": "2026-07-17T23:14:06Z", "sessionId": "s1", "cwd": "/repo",
             "message": {"role": "user", "content": "please fix it"}},
            {"type": "assistant", "timestamp": "2026-07-17T23:15:06Z",
             "message": {"role": "assistant", "model": "claude-opus-4-6",
                         "usage": {"input_tokens": 100, "output_tokens": 50},
                         "content": [{"type": "text", "text": "done"},
                                     {"type": "tool_use", "name": "Edit"}]}},
            {"type": "assistant", "timestamp": "2026-07-17T23:16:06Z",
             "message": {"role": "assistant", "model": "claude-opus-4-6",
                         "usage": {"input_tokens": 10, "output_tokens": 5},
                         "content": [{"type": "text", "text": "verified"}]}},
        ])
        self.assertEqual(st.title, "Fix the flaky test")  # summary wins over first user text
        self.assertEqual(st.cwd, "/repo")
        self.assertEqual(st.model, "claude-opus-4-6")
        self.assertEqual(st.tokens, 165)                  # accumulates across messages
        self.assertEqual(st.tail[-1].text, "verified")


class CodexTest(unittest.TestCase):
    def test_session(self):
        st = feed(CodexAdapter(), [
            {"timestamp": "2026-09-04T16:36:05Z", "type": "session_meta",
             "payload": {"session_id": "c1", "timestamp": "2026-09-04T16:36:03Z", "cwd": "C:\\code"}},
            {"timestamp": "2026-09-04T16:36:06Z", "type": "response_item",
             "payload": {"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": "<environment_context>skip</environment_context>"}]}},
            {"timestamp": "2026-09-04T16:36:07Z", "type": "response_item",
             "payload": {"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": "update the readme"}]}},
            {"timestamp": "2026-09-04T16:40:00Z", "type": "response_item",
             "payload": {"type": "message", "role": "assistant",
                         "content": [{"type": "output_text", "text": "pushed commit 101a19e"}]}},
            {"timestamp": "2026-09-04T16:40:01Z", "type": "event_msg",
             "payload": {"type": "token_count",
                         "info": {"total_token_usage": {"total_tokens": 4846125}}}},
            {"timestamp": "2026-09-04T16:40:02Z", "type": "event_msg",
             "payload": {"type": "task_complete"}},
        ])
        self.assertEqual(st.session_id, "c1")
        self.assertEqual(st.title, "update the readme")   # scaffolding skipped
        self.assertEqual(st.tokens, 4846125)
        self.assertEqual(st.hint, "done")
        self.assertEqual([e.who for e in st.tail], ["user", "assistant"])


if __name__ == "__main__":
    unittest.main()
