"""Synthetic offline diagnostics: no credentials, network, or persistent task state."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import jst_export as export


class CreateDiagnosticsTests(unittest.TestCase):
    def create_failure(self, payload, *, check=False, text=False):
        events = []
        response = type('Response', (), {
            'text': payload if text else json.dumps(payload), 'status_code': 200})()
        def request(*args, **kwargs):
            method = kwargs['params']['am___']
            events.append(method)
            if method == 'ExportCheckConfirm' and not check:
                return type('Response', (), {'text': '{"IsSuccess":true,"ReturnValue":null}'})()
            return response
        with patch.object(export, '_request', side_effect=request):
            with self.assertRaises(export.ExportError) as caught:
                export.create_export(None, {}, {}, ['100'], before_create=lambda: events.append('persist'))
        self.assertEqual(events, ['ExportCheckConfirm'] if check else
                         ['ExportCheckConfirm', 'persist', 'ExportBySync'])
        return caught.exception.safe_diagnostic()

    def test_codes_survive_projection(self):
        for code in ('export_creation_not_confirmed', 'export_confirmation_required'):
            self.assertEqual(export.ExportError('hidden', code=code).safe_diagnostic()['code'], code)
        diagnostic = self.create_failure({'IsSuccess': True, 'ReturnValue': False})
        self.assertEqual(diagnostic['code'], 'export_creation_not_confirmed')
        self.assertEqual(diagnostic['details'], {'export_step': 'create', 'parse_step': 'create_return',
                         'reason': 'creation_not_confirmed', 'type': 'boolean'})
        diagnostic = self.create_failure({'IsSuccess': True, 'ReturnValue': True}, check=True)
        self.assertEqual(diagnostic['code'], 'export_confirmation_required')
        self.assertEqual(diagnostic['details']['export_step'], 'check_confirmation')

    def test_parser_failures_have_static_context(self):
        cases = [('<html>secret</html>', True, 'decode', 'html_or_empty', 'string'),
                 ('999|secret', True, 'decode', 'framing_length_mismatch', 'string'),
                 ('secret', True, 'decode', 'unrecognized_encoding', 'string'),
                 ([], False, 'unwrap', 'unsupported_type', 'array'),
                 ({'Error': 'secret'}, False, 'unwrap', 'server_error', 'object'),
                 ({'IsSuccess': False}, False, 'unwrap', 'explicit_failure', 'object'),
                 ({'ReturnValue': None}, False, 'return_value', 'success_not_confirmed', 'null'),
                 ({'IsSuccess': True, 'ReturnValue': {'secret-key': 'secret'}}, False,
                  'create_return', 'unexpected_return', 'object')]
        for payload, text, step, reason, shape in cases:
            with self.subTest(reason=reason):
                diagnostic = self.create_failure(payload, text=text)
                self.assertEqual(diagnostic['details'], {'export_step': 'create', 'parse_step': step,
                                 'reason': reason, 'type': shape})
                self.assertNotIn('secret', json.dumps(diagnostic))
                self.assertEqual(diagnostic['recovery'], {'action': 'inspect-and-resume', 'requiredResume': True})

    def test_known_markers_do_not_expose_message_or_exception(self):
        for fields, reason in [({'Message': 'secret 910001 secret'}, 'sms_confirmation_marker'),
                               ({'Message': 'not_logged_in'}, 'not_logged_in_marker'),
                               ({'ExceptionMessage': 'https://secret.invalid/?token=secret'}, 'exception_message_present')]:
            diagnostic = self.create_failure(dict(fields, IsSuccess=False, ReturnValue=None))
            self.assertEqual(diagnostic['details']['reason'], reason)
            self.assertNotIn('secret', json.dumps(diagnostic))

    def test_arbitrary_diagnostic_fields_are_dropped(self):
        for key in ('export_step', 'parse_step', 'reason', 'type'):
            for value in ('https://secret.invalid/?token=secret', ['create'], {'create': True}, None, 1):
                with self.subTest(key=key, value=value):
                    result = export.ExportError('secret', details={key: value, 'headers': 'secret',
                                                               'secret-key': 'secret'}).safe_diagnostic()
                    self.assertEqual(result['details'], {})
                    self.assertNotIn('secret', json.dumps(result))

    def test_transport_error_does_not_expose_exception(self):
        session = type('Session', (), {'post': lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError('https://secret.invalid/?token=secret'))})()
        with self.assertRaises(export.ExportError) as caught:
            export._callback(session, {}, export.EXPORT_URL, {}, 'ExportBySync', [])
        self.assertEqual(caught.exception.safe_diagnostic()['details'], {
            'export_step': 'create', 'parse_step': 'request', 'reason': 'request_failed', 'type': 'unknown'})
        self.assertNotIn('secret', str(caught.exception))

    def test_accepted_values_and_unknown_rejection_unchanged(self):
        for checked in (None, False):
            for created in (None, True):
                events = []
                with patch.object(export, '_callback', side_effect=[checked, created]) as callback:
                    export.create_export(None, {}, {}, ['100'], before_create=lambda: events.append('persist'))
                self.assertEqual(callback.call_count, 2)
                self.assertEqual(events, ['persist'])
        for value in ({'ExceptionMessage': 'secret'}, [], 'secret', 0):
            with self.subTest(value=value):
                diagnostic = self.create_failure({'IsSuccess': True, 'ReturnValue': value})
                self.assertEqual(diagnostic['code'], 'export_schema_invalid')


if __name__ == '__main__':
    unittest.main()
