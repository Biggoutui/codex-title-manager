import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import catalog_repair as catalog
import history_titles as history


class CatalogTests(unittest.TestCase):
    def test_filter_preserves_unknown_metadata(self):
        source = {'models': [{'slug': 'unavailable'}, {'slug': 'fallback', 'safety': {'tools': True}}], 'unknown': 42}
        with patch.object(catalog, 'REMOVED_MODEL', 'unavailable'):
            self.assertEqual(catalog.filter_catalog(source), {'models': [source['models'][1]], 'unknown': 42})
        self.assertEqual(len(source['models']), 2)

    def test_atomic_failure_preserves_existing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            catalog.write_json(path, {'pending': True})
            with patch.object(catalog.os, 'replace', side_effect=OSError('injected')):
                with self.assertRaises(OSError):
                    catalog.write_json(path, {'pending': False})
            self.assertEqual(catalog.read_json(path), {'pending': True})
            self.assertEqual(len(list(path.parent.iterdir())), 1)


class FakeRpc:
    def __init__(self, rows):
        self.rows = {r['threadId']: dict(id=r['threadId'], name=r['old_title'], projectId=None,
                     createdAt=1, updatedAt=2, recencyAt=3, status={'type': 'notLoaded'}) for r in rows}
        self.writes = []
        self.timeout_after_commit = False
    def request(self, method, params):
        if method == 'thread/list':
            return {'data': list(self.rows.values()), 'nextCursor': None}
        if method == 'thread/read':
            return {'thread': dict(self.rows[params['threadId']])}
        if method == 'thread/name/set':
            self.writes.append(params)
            self.rows[params['threadId']]['name'] = params['name']
            if self.timeout_after_commit:
                raise TimeoutError()
            return {}
        raise AssertionError(method)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.plan = self.root / 'plan.json'
        self.ledger = self.root / 'ledger.json'
        self.rows = [{'threadId': 'example-thread-' + str(i), 'old_title': 'old ' + str(i),
                      'proposed_title': 'new ' + str(i)} for i in range(4)]
        self.save()
        self.rpc = FakeRpc(self.rows)
    def save(self):
        self.plan.write_text(json.dumps({'renames': self.rows}), encoding='utf-8')
    def run_action(self, action, **kwargs):
        return history.execute(action, self.plan, self.ledger, self.rpc, **kwargs)
    def test_preview_does_not_write_or_create_ledger(self):
        result = self.run_action('preview')
        self.assertEqual(result['eligible'], 4)
        self.assertFalse(self.rpc.writes)
        self.assertFalse(self.ledger.exists())
    def test_batch_and_restore_preserve_later_edit(self):
        self.run_action('apply', batch_size=2)
        self.assertEqual(len(self.rpc.writes), 2)
        self.rpc.rows['example-thread-0']['name'] = 'manual change'
        self.run_action('restore')
        self.assertEqual(self.rpc.rows['example-thread-0']['name'], 'manual change')
        self.assertEqual(self.rpc.rows['example-thread-1']['name'], 'old 1')
    def test_running_and_conflict_are_skipped(self):
        self.rpc.rows['example-thread-0']['status'] = {'type': 'active'}
        self.rpc.rows['example-thread-1']['name'] = 'manual'
        self.run_action('apply', batch_size=4)
        self.assertEqual(len(self.rpc.writes), 2)
    def test_unknown_response_reconciles_committed_write(self):
        self.rpc.timeout_after_commit = True
        self.run_action('apply', batch_size=1)
        self.assertEqual(json.loads(self.ledger.read_text())['entries'][0]['state'], 'applied')
    def test_plan_change_rejected(self):
        self.run_action('apply', batch_size=1)
        self.rows[1]['proposed_title'] = 'changed'
        self.save()
        with self.assertRaises(history.SafetyStop):
            self.run_action('apply')
    def test_null_name_skipped_without_explicit_baseline(self):
        self.rpc.rows['example-thread-0']['name'] = None
        self.run_action('apply', batch_size=1)
        self.assertFalse(self.rpc.writes)
    def test_duplicate_id_rejected(self):
        self.rows.append(dict(self.rows[0]))
        self.save()
        with self.assertRaises(history.SafetyStop):
            self.run_action('preview')

    def test_metadata_change_stops_further_writes(self):
        request = self.rpc.request
        def changed(method, params):
            result = request(method, params)
            if method == 'thread/name/set':
                self.rpc.rows[params['threadId']]['recencyAt'] += 1
            return result
        self.rpc.request = changed
        with self.assertRaises(history.SafetyStop):
            self.run_action('apply')
        self.assertEqual(len(self.rpc.writes), 1)

    def test_pending_recovered_after_readback_failure(self):
        request = self.rpc.request
        def failing(method, params):
            if method == 'thread/read' and self.rpc.writes:
                raise TimeoutError()
            return request(method, params)
        self.rpc.request = failing
        with self.assertRaises(history.SafetyStop):
            self.run_action('apply', batch_size=1)
        self.assertEqual(json.loads(self.ledger.read_text())['entries'][0]['state'], 'pending')
        self.rpc.request = request
        self.run_action('apply', batch_size=1)
        self.assertEqual(len(self.rpc.writes), 2)


class ConfigOwnershipTests(unittest.TestCase):
    def test_commit_then_timeout_restorable_with_later_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private = root / 'private'
            private.mkdir()
            config = root / 'config.toml'
            config.write_text('model="fallback"\n', encoding='utf8')
            candidate = root / 'catalog.json'
            candidate.write_text('{}', encoding='utf8')
            evidence = {'passed': True, 'catalog_sha256': catalog.digest(candidate.read_bytes())}
            catalog.write_json(root / 'verification.json', evidence)
            values = {'model': 'fallback'}
            class Native:
                def __init__(self, *args): pass
                def read(self): return dict(values), 'fresh-version'
                def write(self, value, version):
                    self.assert_version(version)
                    if value is None: values.pop(catalog.KEY, None)
                    else:
                        values[catalog.KEY] = value
                        raise TimeoutError()
                def assert_version(self, version):
                    if version != 'fresh-version': raise ValueError('stale')
                def close(self): pass
            attributes = {'ROOT': root, 'CONFIG': config, 'PRIVATE': private, 'STATE': private/'state.json',
                          'CATALOG': candidate}
            with patch.multiple(catalog, **attributes), patch.object(catalog, 'validate_source', return_value=dict(evidence, exe='example-cli')), \
                 patch.object(catalog, 'NativeConfig', Native), patch.object(catalog, 'private_dir'), \
                 patch.object(catalog, 'active_cli', return_value=Path('example-cli')):
                with self.assertRaises(TimeoutError): catalog.apply()
                self.assertTrue(catalog.read_json(catalog.STATE)['pending'])
                values['later'] = True
                self.assertTrue(catalog.restore()['restored'])
                self.assertTrue(values['later'])
                self.assertNotIn(catalog.KEY, values)


if __name__ == '__main__':
    unittest.main()
