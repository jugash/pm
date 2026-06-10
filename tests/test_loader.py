import tempfile
import unittest
from pathlib import Path

import yaml

from perfbench.config.loader import expand_matrix, get_path, load_scenarios, set_path
from perfbench.errors import SchemaError
from tests.helpers import BASE_SCENARIO


def _matrix(**overrides):
    base = {k: v for k, v in BASE_SCENARIO.items() if k not in ("id", "onload")}
    # id_template paths must exist in the document, so spell out the default
    base["nic"] = {**base["nic"], "bond_mode": "none"}
    spec = {
        "id_template": "bm-{network_path}-{nic.bond_mode}",
        "base": base,
        "axes": {
            "network_path": ["kernel", "onload"],
            "cpu.client_cores.0": [2, 8],
        },
    }
    spec.update(overrides)
    return spec


class TestPaths(unittest.TestCase):
    def test_set_path_nested_create(self):
        doc = {}
        set_path(doc, "a.b.c", 5)
        self.assertEqual(doc, {"a": {"b": {"c": 5}}})

    def test_set_path_list_index(self):
        doc = {"nic": {"ports": [{"name": "x"}]}}
        set_path(doc, "nic.ports.0.name", "y")
        self.assertEqual(doc["nic"]["ports"][0]["name"], "y")

    def test_get_path(self):
        doc = {"a": [{"b": 1}]}
        self.assertEqual(get_path(doc, "a.0.b"), 1)


class TestMatrix(unittest.TestCase):
    def test_expansion_count_and_ids(self):
        scenarios = expand_matrix(
            _matrix(id_template="bm-{network_path}-{cpu.client_cores.0}")
        )
        self.assertEqual(len(scenarios), 4)
        ids = [s.id for s in scenarios]
        self.assertIn("bm-kernel-2", ids)
        self.assertIn("bm-onload-8", ids)
        # axis value applied
        kernels = [s for s in scenarios if s.network_path.value == "kernel"]
        self.assertEqual({s.cpu.client_cores[0] for s in kernels}, {2, 8})

    def test_id_template_unknown_path(self):
        with self.assertRaises(SchemaError):
            expand_matrix(_matrix(id_template="x-{nonexistent.path}"))

    def test_duplicate_ids_from_template(self):
        # id template ignores the differing axis -> duplicate ids -> caught by load
        spec = _matrix(id_template="same-{network_path}")
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "m.yaml"
            f.write_text(yaml.safe_dump({"matrices": [spec]}))
            with self.assertRaises(SchemaError) as ctx:
                load_scenarios(f)
            self.assertIn("duplicate", str(ctx.exception))

    def test_matrix_validation_errors(self):
        with self.assertRaises(SchemaError):
            expand_matrix("not a mapping")
        with self.assertRaises(SchemaError):
            expand_matrix(_matrix(bogus=1))
        with self.assertRaises(SchemaError):
            expand_matrix({"id_template": "x", "axes": {"a": [1]}})  # no base
        with self.assertRaises(SchemaError):
            expand_matrix({"base": {}, "axes": {"a": [1]}})  # no template
        with self.assertRaises(SchemaError):
            expand_matrix({"base": {}, "id_template": "x"})  # no axes
        with self.assertRaises(SchemaError):
            expand_matrix(_matrix(axes={"network_path": []}))  # empty axis


class TestLoadScenarios(unittest.TestCase):
    def test_load_file_with_scenarios_and_matrix(self):
        doc = {
            "scenarios": [BASE_SCENARIO],
            "matrices": [_matrix(
                id_template="bm-{network_path}-{cpu.client_cores.0}"
            )],
        }
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "s.yaml"
            f.write_text(yaml.safe_dump(doc))
            scenarios = load_scenarios(f)
        self.assertEqual(len(scenarios), 5)

    def test_load_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a.yaml").write_text(
                yaml.safe_dump({"scenarios": [BASE_SCENARIO]})
            )
            other = {**BASE_SCENARIO, "id": "bm-other"}
            (Path(tmp) / "b.yaml").write_text(yaml.safe_dump({"scenarios": [other]}))
            scenarios = load_scenarios(tmp)
        self.assertEqual({s.id for s in scenarios}, {"bm-onload-test", "bm-other"})

    def test_load_empty_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SchemaError):
                load_scenarios(tmp)

    def test_missing_path(self):
        with self.assertRaises(SchemaError):
            load_scenarios("/nonexistent/path.yaml")

    def test_invalid_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "bad.yaml"
            f.write_text("scenarios: [unclosed")
            with self.assertRaises(SchemaError):
                load_scenarios(f)

    def test_top_level_must_be_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "list.yaml"
            f.write_text("- 1\n- 2\n")
            with self.assertRaises(SchemaError):
                load_scenarios(f)

    def test_unknown_top_level_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "k.yaml"
            f.write_text(yaml.safe_dump({"scenario": [BASE_SCENARIO]}))
            with self.assertRaises(SchemaError):
                load_scenarios(f)

    def test_empty_file_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "empty.yaml"
            f.write_text("")
            self.assertEqual(load_scenarios(f), [])


if __name__ == "__main__":
    unittest.main()
