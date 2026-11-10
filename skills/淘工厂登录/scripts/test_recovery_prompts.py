"""Offline regression tests: recovery guidance and existing gate semantics.

No real browser, credentials, cookie database or remote service is accessed.
Run: python -B -m unittest discover -s <skill>/scripts -p test_recovery_prompts.py -v
"""
import contextlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import browser_bridge
import ensure_login
import tmall_session


class RecoveryPromptTests(unittest.TestCase):
    def test_recovery_actions_use_fixed_route(self):
        for reason in ("no-cookie", "no-session", "expired", "wrong-account", "wrong-company"):
            with self.subTest(reason=reason):
                text = ensure_login.ACTION[reason]
                for word in ("tab_new", "自己的 Magpie", "已登录则不要退出", "确认未登录",
                             "Playwright Extension", "服务端公司验证", "验证码", "不回显"):
                    self.assertIn(word, text)
                for stale in ("两条路任选", "asset-inventory", "复用同一标签", "<script src="):
                    self.assertNotIn(stale, text)
                self.assertIn("同一次恢复", text)

    def test_no_session_is_not_logged_out(self):
        self.assertIn("no-session 不等于未登录", ensure_login.ACTION["no-session"])

    def test_company_unknown_never_approves_visual_check(self):
        self.assertIn("不能宣告", ensure_login.ACTION["company-unknown"])
        self.assertIn("页面公司名不能代替服务端验证", ensure_login.ACTION["company-unknown"])

    def test_logout_authorization_has_planned_and_unexpected_boundaries(self):
        for reason in ("wrong-account", "wrong-company"):
            for word in ("计划内切换无需再问", "经用户确认", "刚完成的上一家公司", "不扩展为ERP换号授权"):
                self.assertIn(word, ensure_login.ACTION[reason])

    def test_form_guidance_preserves_autofill_and_waits(self):
        for word in ("补账号后再次检查密码是否有值", "保留自动填充", "只询问一次", "无新输入不重复催问",
                     "不得把含凭据历史恢复到临时文件"):
            self.assertIn(word, ensure_login.RECOVERY_ROUTE)

    def test_bridge_prompt_uses_owned_tab_and_source(self):
        text = browser_bridge.AGENT_HINT
        for word in ("不等于未登录", "tab_new", "read", "evaluate", "服务端公司验证", "不复用用户"):
            self.assertIn(word, text)
        self.assertNotIn("即成功", text)

    def test_cookie_unavailable_does_not_assert_logged_out(self):
        with patch.object(tmall_session, "cookie_files", return_value=[]):
            with self.assertRaises(tmall_session.SessionError) as caught:
                tmall_session.build_headers()
        text = str(caught.exception)
        for word in ("不代表浏览器未登录", "Magpie", "Playwright Extension", "凭据"):
            self.assertIn(word, text)
        self.assertNotIn("复用已有标签", text)

    def test_v20_guidance_keeps_security_settings(self):
        with self.assertRaises(tmall_session.SessionError) as caught:
            tmall_session._decrypt(b"v20fake", b"unused", "tgc.tmall.com")
        self.assertIn("不更改浏览器加密设置", str(caught.exception))
        self.assertNotIn("关闭应用绑定加密", str(caught.exception))

    def test_bridge_error_still_yields_no_session(self):
        with patch.object(tmall_session, "build_session", side_effect=tmall_session.BridgeSessionError("offline")):
            state = ensure_login.probe("auto", tmall_session.DEFAULT_DOMAIN)
        self.assertFalse(state["loggedIn"])
        self.assertEqual(state["reason"], "no-session")
        self.assertEqual(state["action"], ensure_login.ACTION["no-session"])

    def run_gate(self, company):
        state = {"loggedIn": True, "reason": "logged-in", "detail": "", "accountHint": "synthetic",
                 "erpMobile": "", "company": company, "companyDetail": "", "action": "ok"}
        output = io.StringIO()
        with patch.object(ensure_login, "probe", return_value=state), contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            code = ensure_login.main(["--expect-company", "目标公司"])
        return code, json.loads(output.getvalue())

    def test_server_company_gate_preserved(self):
        for company, code, reason in (("目标公司有限公司", 0, "logged-in"),
                                      ("别家公司", 3, "wrong-company"), ("", 3, "company-unknown")):
            with self.subTest(company=company):
                actual_code, state = self.run_gate(company)
                self.assertEqual(actual_code, code)
                self.assertEqual(state["reason"], reason)

    def test_scope_recovery_explicit(self):
        self.assertIn("新的内存 scope", ensure_login.RECOVERY_ROUTE)
        self.assertNotIn("bridge_agent.js", ensure_login.RECOVERY_ROUTE)


if __name__ == "__main__":
    unittest.main()
