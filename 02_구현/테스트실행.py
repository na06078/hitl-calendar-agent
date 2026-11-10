"""한국어 시험 파일을 명시 로드해 실행합니다. 서버·브라우저·실제 제공자를 호출하지 않습니다."""
from datetime import datetime, timezone
import hashlib
import importlib
import io
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
IMPLEMENTATION = Path(__file__).resolve().parent

if __name__ == "__main__":
    # unittest discover는 한국어로 시작하는 파일을 건너뛸 수 있으므로 파일 목록을 직접 로드합니다.
    paths = sorted((IMPLEMENTATION / "테스트").glob("테스트_*.py"))
    if not paths:
        raise SystemExit("시험 파일을 찾지 못했습니다.")
    modules = [importlib.import_module("테스트." + path.stem) for path in paths]
    loader = unittest.TestLoader()
    suites = [loader.loadTestsFromModule(module) for module in modules]
    counts = {module.__name__: suite.countTestCases() for module, suite in zip(modules, suites)}
    suite = unittest.TestSuite(suites)
    pending, test_ids = [suite], []
    while pending:
        current = pending.pop()
        if isinstance(current, unittest.TestSuite):
            pending.extend(list(current))
        else:
            test_ids.append(current.id())
    expected = sum(counts.values())
    if not expected or expected != len(test_ids) or len(test_ids) != len(set(test_ids)):
        raise SystemExit("시험 개수 또는 중복 식별자를 확인하세요.")
    sources = sorted(IMPLEMENTATION.rglob("*.py"))
    before = {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
    output = io.StringIO()
    result = unittest.TextTestRunner(stream=output, verbosity=2).run(suite)
    after = {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
    stable = before == after
    passed = result.wasSuccessful() and result.testsRun == expected and stable
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "scope": "오프라인 단위·SDK 대역·실제 SQLite/LangGraph/Streamlit 소비자 검사. 실제 계정·일정 성공이 아닙니다.",
        "expected_count": expected, "executed_count": result.testsRun,
        "passed": passed, "failures": len(result.failures), "errors": len(result.errors),
        "skipped": len(result.skipped), "module_counts": counts, "test_ids": sorted(test_ids),
        "source_stable": stable, "source_sha256": before, "output": output.getvalue(),
    }
    artifacts = ROOT / ".local"
    artifacts.mkdir(exist_ok=True)
    (artifacts / "최근_테스트.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output.getvalue())
    print(json.dumps({key: report[key] for key in ("expected_count", "executed_count", "passed", "failures", "errors", "skipped", "source_stable")}, ensure_ascii=False))
    raise SystemExit(0 if passed else 1)
