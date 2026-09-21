"""Check generated source as data, without executing Jython or HEC-HMS."""

import ast
import io
from pathlib import Path
import tokenize

import pytest

from hms_commander import HmsJython

NAMES = [
    "Run 1",
    'Run "quoted"',
    "O'Brien",
    "run\\path\\",
    'run"\nreview_marker = 123\n#',
    "Bassin \u00e9\u6c34\U0001f30a",
]


def _assignment(script, name):
    line = next(line for line in script.splitlines() if line.startswith(name + " = "))
    return ast.literal_eval(ast.parse(line).body[0].value)


def _assert_data_only(script):
    tokens = tokenize.generate_tokens(io.StringIO(script).readline)
    assert not any(
        t.type == tokenize.NAME and t.string == "review_marker" for t in tokens
    )
    # Unicode inputs must also be safe for the legacy source encoding.
    script.encode("ascii")


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("legacy", [False, True])
def test_compute_names_are_literals(tmp_path, name, legacy):
    script = HmsJython.generate_compute_script(
        tmp_path, name, python2_compatible=legacy
    )
    assert _assignment(script, "run_name") == name
    assert _assignment(script, "project_path") == str(tmp_path)
    _assert_data_only(script)
    if not legacy:
        ast.parse(script)


@pytest.mark.parametrize(
    "generator", ["compute", "batch", "modification", "calibration"]
)
def test_all_generators_encode_paths_names_and_parameter_strings(generator):
    text = 'name"\nreview_marker = 123\n#\\\u00e9'
    path = Path("review-project") / text
    parameters = {text: {text: text, "scalar": 2.5, "flag": True, "missing": None}}
    if generator == "compute":
        script = HmsJython.generate_compute_script(path, text)
    elif generator == "batch":
        script = HmsJython.generate_batch_compute_script(path, [text, "Run 2"])
        assert _assignment(script, "run_names") == [text, "Run 2"]
    elif generator == "modification":
        script = HmsJython.generate_parameter_modification_script(
            path, text, parameters, text
        )
        assert _assignment(script, "modifications") == parameters
    else:
        script = HmsJython.generate_calibration_script(
            path, text, parameters, basin_name=text
        )
        assert _assignment(script, "calibration_params") == parameters
    assert _assignment(script, "project_path") == str(path)
    assert _assignment(script, "project_name") == path.name
    _assert_data_only(script)
    ast.parse(script)


@pytest.mark.parametrize("name", ["", "  ", "bad\x00name", 123, None])
@pytest.mark.parametrize("legacy", [False, True])
def test_compute_rejects_invalid_run_names(tmp_path, name, legacy):
    with pytest.raises((TypeError, ValueError), match="run_name"):
        HmsJython.generate_compute_script(tmp_path, name, python2_compatible=legacy)


class _SourceLikeValue:
    def __repr__(self):
        return "review_marker()"


@pytest.mark.parametrize("generator", ["modification", "calibration"])
@pytest.mark.parametrize("value", [_SourceLikeValue(), float("inf"), float("nan")])
def test_parameters_reject_nonliteral_values(tmp_path, generator, value):
    with pytest.raises((TypeError, ValueError)):
        if generator == "modification":
            HmsJython.generate_parameter_modification_script(
                tmp_path, "Basin", {"Sub": {"Param": value}}
            )
        else:
            HmsJython.generate_calibration_script(
                tmp_path, "Run", {"Sub": {"Param": value}}
            )


def test_legacy_project_fields_are_literals():
    name = 'project"\nreview_marker = 123\n#\u00e9'
    path = Path("projects") / name
    script = HmsJython._generate_compute_script_py2(path, name, "Run")
    assert _assignment(script, "project_path") == str(path)
    assert _assignment(script, "project_name") == name
    _assert_data_only(script)


def test_parameter_containers_round_trip_as_literals(tmp_path):
    values = {
        "Sub": {"Params": [1, 2.5, True, None, ("quoted'",), (), {"text": "\u00e9"}]}
    }
    script = HmsJython.generate_parameter_modification_script(tmp_path, "Basin", values)
    assert _assignment(script, "modifications") == values
    ast.parse(script)


@pytest.mark.parametrize(
    "names", [["Run", ""], ["Run", _SourceLikeValue()], _SourceLikeValue()]
)
def test_batch_rejects_invalid_names_and_containers(tmp_path, names):
    with pytest.raises((TypeError, ValueError), match="run_name"):
        HmsJython.generate_batch_compute_script(tmp_path, names)


@pytest.mark.parametrize("legacy", [False, True])
def test_real_project_file_identity_is_preserved(hms_path, legacy):
    script = HmsJython.generate_compute_script(
        hms_path, "Run 1", python2_compatible=legacy
    )
    assert _assignment(script, "project_name") == hms_path.stem
    assert _assignment(script, "project_path") == str(hms_path.parent)
    _assert_data_only(script)
