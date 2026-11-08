"""Offline regression for the inspected live _ACP protocol; never creates ERP tasks."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import jst_export as export

class CurrentCreateProtocolTests(unittest.TestCase):
    def test_boolean_envelope(self):
        for value in (True, False):
            response = type('Response', (), {'text': json.dumps({'IsSuccess': True, 'ReturnValue': value})})()
            self.assertIs(export._unwrap(response), value)

    def test_dispatch_and_durable_intent(self):
        events = []
        def callback(*args, **kwargs):
            self.assertEqual(kwargs['callback_id'], 'JTable1')
            self.assertEqual(kwargs['control'], '{page}')
            events.append(args[4])
            return None if args[4] == 'ExportCheckConfirm' else True
        with patch.object(export, '_callback', side_effect=callback):
            export.create_export(None, {}, {}, ['100'], before_create=lambda: events.append('persist'))
        self.assertEqual(events, ['ExportCheckConfirm', 'persist', 'ExportBySync'])

    def test_paid_confirmation_stops_before_intent(self):
        with patch.object(export, '_callback', return_value=True) as callback:
            with self.assertRaises(export.ExportError) as caught:
                export.create_export(None, {}, {}, ['100'], before_create=lambda: self.fail('must not persist'))
            self.assertEqual(caught.exception.code, 'export_confirmation_required')
            self.assertEqual(callback.call_count, 1)

    def test_false_create_is_not_success(self):
        with patch.object(export, '_callback', side_effect=[None, False]):
            with self.assertRaises(export.ExportError):
                export.create_export(None, {}, {}, ['100'], before_create=lambda: None)

if __name__ == '__main__':
    unittest.main()
