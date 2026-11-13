import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('builder', Path(__file__).resolve().parents[1]/'tools'/'build_release.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class ReleaseTests(unittest.TestCase):
    def test_known_private_value_detected_without_echo(self):
        self.assertIn('known-private-value', builder.scan(b'prefix PRIVATE_MARKER suffix', ['PRIVATE_MARKER']))
    def test_secret_pattern_detected(self):
        self.assertIn('secret-token', builder.scan(('sk-' + 'x'*32).encode(), []))
    def test_user_path_detected(self):
        value = b'C:' + bytes([92]) + b'Users' + bytes([92]) + b'example-person' + bytes([92]) + b'config'
        self.assertIn('personal-path', builder.scan(value, []))
    def test_binary_refused(self):
        self.assertIn('non-text', builder.scan(b'\x00\xff', []))
    def test_safe_example_allowed(self):
        self.assertEqual(builder.scan(b'{"threadId":"00000000-0000-4000-8000-000000000001"}', []), [])


if __name__ == '__main__': unittest.main()
