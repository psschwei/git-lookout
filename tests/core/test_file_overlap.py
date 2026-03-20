from git_lookout.core.git_manager import file_overlap


def test_overlap_with_common_files():
    assert set(file_overlap(["a.py", "b.py", "c.py"], ["b.py", "c.py", "d.py"])) == {"b.py", "c.py"}


def test_overlap_no_common_files():
    assert file_overlap(["a.py"], ["b.py"]) == []


def test_overlap_empty_inputs():
    assert file_overlap([], ["a.py"]) == []
    assert file_overlap(["a.py"], []) == []
    assert file_overlap([], []) == []


def test_overlap_identical_lists():
    files = ["a.py", "b.py"]
    assert set(file_overlap(files, files)) == set(files)
