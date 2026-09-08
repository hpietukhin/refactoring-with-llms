"""Tests for CK metrics GradleRunner and CSV report parsing."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from git import Repo as GitRepo

from java.metrics.ck_reports import (
    parse_ck_class_csv,
    parse_ck_method_csv,
    run_ck_metrics,
)
from java.metrics.runner import GradleRunner
from repository.repo import Repo


_CLASS_CSV = """file,class,type,cbo,cboModified,fanin,fanout,wmc,dit,noc,rfc,lcom,lcom*,tcc,lcc,totalMethodsQty,staticMethodsQty,publicMethodsQty,privateMethodsQty,protectedMethodsQty,defaultMethodsQty,visibleMethodsQty,abstractMethodsQty,finalMethodsQty,synchronizedMethodsQty,totalFieldsQty,staticFieldsQty,publicFieldsQty,privateFieldsQty,protectedFieldsQty,defaultFieldsQty,finalFieldsQty,synchronizedFieldsQty,nosi,loc,returnQty,loopQty,comparisonsQty,tryCatchQty,parenthesizedExpsQty,stringLiteralsQty,numbersQty,assignmentsQty,mathOperationsQty,variablesQty,maxNestedBlocksQty,anonymousClassesQty,innerClassesQty,lambdasQty,uniqueWordsQty,modifiers,logStatementsQty
/tmp/A.java,com.Example,class,2,0,0,0,3,1,0,4,1,0.0,0.0,0.0,2,0,2,0,0,0,2,0,0,0,1,0,0,1,0,0,0,0,0,10,1,0,0,0,0,0,0,0,0,0,1,0,0,0,5,1,0
/tmp/B.java,com.Other,class,4,0,0,0,5,2,0,6,3,0.0,0.0,0.0,3,0,3,0,0,0,3,0,0,0,2,0,0,2,0,0,0,0,0,20,2,0,0,0,0,0,0,0,0,0,1,0,0,0,8,1,0
"""

_METHOD_CSV = """file,class,method,constructor,line,cbo,cboModified,fanin,fanout,wmc,rfc,loc,returnsQty,variablesQty,parametersQty,methodsInvokedQty,methodsInvokedLocalQty,methodsInvokedIndirectLocalQty,loopQty,comparisonsQty,tryCatchQty,parenthesizedExpsQty,stringLiteralsQty,numbersQty,assignmentsQty,mathOperationsQty,maxNestedBlocksQty,anonymousClassesQty,innerClassesQty,lambdasQty,uniqueWordsQty,modifiers,logStatementsQty,hasJavaDoc
/tmp/A.java,com.Example,ok/0,false,12,1,0,0,0,1,1,4,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,2,1,0,false
"""


def _init_repo(root: Path) -> Repo:
    (root / "pom.xml").write_text("<project></project>\n", encoding="utf-8")
    git = GitRepo.init(root)
    git.index.add(["pom.xml"])
    git.index.commit("init")
    return Repo(root, git_repo=git)


def test_parse_ck_class_csv(tmp_path: Path) -> None:
    path = tmp_path / "class.csv"
    path.write_text(_CLASS_CSV, encoding="utf-8")
    rows = parse_ck_class_csv(path)
    assert len(rows) == 2
    assert rows[0].class_name == "com.Example"
    assert rows[0].cbo == 2.0
    assert rows[1].lcom == 3.0
    assert rows[0].extras["fanin"] == "0"


def test_parse_ck_method_csv(tmp_path: Path) -> None:
    path = tmp_path / "method.csv"
    path.write_text(_METHOD_CSV, encoding="utf-8")
    rows = parse_ck_method_csv(path)
    assert len(rows) == 1
    assert rows[0].method == "ok/0"
    assert rows[0].line == 12
    assert rows[0].wmc == 1.0


def test_run_ck_metrics_uses_runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _init_repo(tmp_path)
    out = tmp_path / "ck-out"
    out.mkdir()
    (out / "class.csv").write_text(_CLASS_CSV, encoding="utf-8")
    (out / "method.csv").write_text(_METHOD_CSV, encoding="utf-8")

    def fake_collect(
        self: GradleRunner,
        output_dir: str | Path,
        *,
        use_jars: bool = False,
        max_files: int = 0,
        variables_and_fields: bool = False,
        ignored_directories: object = (),
        ensure_package: bool = True,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del (
            self,
            use_jars,
            max_files,
            variables_and_fields,
            ignored_directories,
            ensure_package,
            timeout,
        )
        assert Path(output_dir) == out
        return subprocess.CompletedProcess(["java", "-jar", "ck.jar"], 0, stdout="ok", stderr="")

    monkeypatch.setattr("java.metrics.runner.GradleRunner.collect", fake_collect)
    summary = run_ck_metrics(repo, output_dir=out, ensure_package=False)
    assert summary.success
    assert summary.totals.classes == 2
    assert summary.totals.methods == 1
    assert summary.totals.mean_cbo == 3.0
    assert summary.totals.mean_lcom == 2.0
    assert summary.totals.total_loc == 30.0


def test_maven_runner_fat_jar_missing(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    ck_dir = tmp_path / "ck-tool"
    ck_dir.mkdir()
    (ck_dir / "pom.xml").write_text("<project></project>\n", encoding="utf-8")
    runner = GradleRunner(repo, ck_dir=ck_dir)
    with pytest.raises(RuntimeError, match="fat jar not found"):
        runner.fat_jar()
