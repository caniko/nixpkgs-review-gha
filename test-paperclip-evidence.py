import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('evidence', Path(__file__).with_name('paperclip-evidence.py'))
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)


class SourceBindingTests(unittest.TestCase):
    def records(self):
        return [{'head': {'sha': 'a' * 40}, 'merged': False},
                {'parents': [{'sha': evidence.PARENT}], 'commit': {'verification': {'verified': True}},
                 'files': [{'filename': path} for path in sorted(evidence.SOURCE_PATHS)]}]

    def test_exact_signed_head_parent_and_paths_are_bound(self):
        with patch.object(evidence, 'api', side_effect=self.records()):
            source = evidence.source_identity('a' * 40, evidence.PARENT, 'b' * 64)
        self.assertEqual(source['source_head'], 'a' * 40)
        self.assertEqual(source['source_parent'], evidence.PARENT)
        self.assertEqual(set(source['source_paths']), evidence.SOURCE_PATHS)

    def test_stale_unsigned_foreign_parent_and_out_of_scope_source_fail_closed(self):
        for kind in ['stale', 'unsigned', 'foreign-parent', 'foreign-path']:
            records = self.records()
            if kind == 'stale': records[0]['head']['sha'] = 'c' * 40
            if kind == 'unsigned': records[1]['commit']['verification']['verified'] = False
            if kind == 'foreign-parent': records[1]['parents'][0]['sha'] = 'c' * 40
            if kind == 'foreign-path': records[1]['files'].append({'filename': 'flake.lock'})
            with self.subTest(kind=kind), patch.object(evidence, 'api', side_effect=records):
                with self.assertRaises(RuntimeError):
                    evidence.source_identity('a' * 40, evidence.PARENT, 'b' * 64)

    def test_no_review_binding_or_unselected_source_cannot_schedule(self):
        for head, parent, review in [('main', evidence.PARENT, 'b' * 64),
                                     (evidence.PARENT, evidence.PARENT, 'b' * 64),
                                     ('a' * 40, 'c' * 40, 'b' * 64),
                                     ('a' * 40, evidence.PARENT, '')]:
            with self.subTest(head=head, parent=parent, review=review):
                with self.assertRaises(RuntimeError):
                    evidence.source_identity(head, parent, review)


if __name__ == '__main__':
    unittest.main()
